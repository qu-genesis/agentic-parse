# Entity Names Extraction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract named people and organizations from every document, normalize to lowercase canonical forms, resolve aliases globally (e.g. "Safari Restaurant LLC" == "safari restaurant"), and expose results for per-doc display and cross-document search.

**Architecture:** Two-phase pipeline stage `extract_entity_names` in a new `entity_names.py` module. Phase 1 runs one focused LLM call per document (parallel, 16 workers) to extract raw person/org strings. Phase 2 aggregates all raw strings, pre-deduplicates cheaply, then runs batched LLM canonicalization to build a global entity registry.

**Tech Stack:** Python, OpenAI (gpt-4o-mini via existing `LLMClient`), psycopg2, ThreadPoolExecutor, existing `chunk_embed.retrieve_top_k_chunks`, `utils.write_json`, `utils.atomic_write_text`.

---

### Task 1: Add config paths and ensure_dirs

**Files:**
- Modify: `src/agentic_parse/config.py`

**Step 1: Write the failing test**

```python
# tests/test_entity_names.py  (new file, add just this test for now)
from pathlib import Path
from agentic_parse.config import Settings

def test_config_entity_names_paths(tmp_path: Path) -> None:
    s = Settings(workspace=tmp_path, raw_root=tmp_path / "raw")
    assert s.entity_names_dir == tmp_path / "outputs" / "entity_names"
    assert s.entity_registry_json == tmp_path / "outputs" / "entity_registry.json"
    assert s.entity_registry_jsonl == tmp_path / "outputs" / "entity_registry.jsonl"
    s.ensure_dirs()
    assert s.entity_names_dir.exists()
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_entity_names.py::test_config_entity_names_paths -v
```

Expected: `AttributeError: 'Settings' object has no attribute 'entity_names_dir'`

**Step 3: Add properties to Settings**

In `src/agentic_parse/config.py`, add after the `entities_dir` property:

```python
@property
def entity_names_dir(self) -> Path:
    return self.workspace / "outputs" / "entity_names"

@property
def entity_registry_json(self) -> Path:
    return self.workspace / "outputs" / "entity_registry.json"

@property
def entity_registry_jsonl(self) -> Path:
    return self.workspace / "outputs" / "entity_registry.jsonl"
```

In `ensure_dirs`, add `self.entity_names_dir` to the `dirs` list.

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_entity_names.py::test_config_entity_names_paths -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add src/agentic_parse/config.py tests/test_entity_names.py
git commit -m "feat: add entity_names config paths"
```

---

### Task 2: Add DB column via init_schema

**Files:**
- Modify: `src/agentic_parse/db.py`

**Step 1: Write the failing test**

Add to `tests/test_entity_names.py`:

```python
from agentic_parse.db import init_schema, connect

def test_db_has_status_entity_names_column(tmp_path: Path) -> None:
    import os
    dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/agentic_parse")
    conn = connect(dsn)
    init_schema(conn)
    # Should not raise; column must exist after init
    conn.execute("SELECT status_entity_names FROM documents LIMIT 0")
    conn.close()
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_entity_names.py::test_db_has_status_entity_names_column -v
```

Expected: `psycopg2.errors.UndefinedColumn` or similar.

**Step 3: Add column to init_schema**

In `src/agentic_parse/db.py`, inside `init_schema`, after the existing `_ensure_column` calls (or add a new one at the end of the function):

```python
_ensure_column(
    conn,
    "documents",
    "status_entity_names",
    "TEXT NOT NULL DEFAULT 'pending'",
)
```

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_entity_names.py::test_db_has_status_entity_names_column -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add src/agentic_parse/db.py tests/test_entity_names.py
git commit -m "feat: add status_entity_names column to documents"
```

---

### Task 3: Pure extraction helpers (context builder + LLM call)

**Files:**
- Create: `src/agentic_parse/entity_names.py`

**Step 1: Write the failing tests**

Add to `tests/test_entity_names.py`:

```python
import json
from pathlib import Path
from agentic_parse.entity_names import _build_extraction_prompt, _parse_extraction_response

def test_build_extraction_prompt_includes_summary_and_chunks() -> None:
    prompt = _build_extraction_prompt(
        summary_text="John Smith works at Safari Restaurant LLC.",
        chunks_text="Mary O'Brien is the manager.",
    )
    assert "John Smith" in prompt
    assert "Mary O'Brien" in prompt
    assert "persons" in prompt
    assert "organizations" in prompt

def test_parse_extraction_response_valid() -> None:
    raw = {"persons": ["John Smith", "J. Smith"], "organizations": ["Safari Restaurant LLC"]}
    result = _parse_extraction_response(raw)
    assert result["persons"] == ["John Smith", "J. Smith"]
    assert result["organizations"] == ["Safari Restaurant LLC"]

def test_parse_extraction_response_tolerates_missing_keys() -> None:
    result = _parse_extraction_response({"persons": ["Alice"]})
    assert result["organizations"] == []

def test_parse_extraction_response_returns_empty_on_none() -> None:
    result = _parse_extraction_response(None)
    assert result == {"persons": [], "organizations": []}
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_entity_names.py -k "extraction_prompt or extraction_response" -v
```

Expected: `ImportError: cannot import name '_build_extraction_prompt'`

**Step 3: Create entity_names.py with these pure helpers**

```python
from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_costly_call, record_stage_metric
from .utils import atomic_write_text, write_json

ENTITY_NAMES_MAX_WORKERS = int(os.getenv("ENTITY_NAMES_MAX_WORKERS", "16"))
_RESOLUTION_BATCH_SIZE = 100
_CONTEXT_MAX_CHARS = 4000


def _build_extraction_prompt(*, summary_text: str, chunks_text: str) -> str:
    context = f"{summary_text}\n\n{chunks_text}".strip()
    if len(context) > _CONTEXT_MAX_CHARS:
        context = context[:_CONTEXT_MAX_CHARS]
    return (
        "Extract all named persons and organizations from the following document context.\n"
        'Return {"persons": ["Full Name", ...], "organizations": ["Org Name", ...]}\n'
        "Include every variant or abbreviation you see — do not normalize.\n"
        "If none found, return empty lists.\n\n"
        f"CONTEXT:\n{context}\n\n"
        "Return JSON only."
    )


def _parse_extraction_response(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {"persons": [], "organizations": []}
    persons = payload.get("persons")
    orgs = payload.get("organizations")
    return {
        "persons": [str(v).strip() for v in persons if str(v).strip()] if isinstance(persons, list) else [],
        "organizations": [str(v).strip() for v in orgs if str(v).strip()] if isinstance(orgs, list) else [],
    }
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_entity_names.py -k "extraction_prompt or extraction_response" -v
```

Expected: 4 PASS

**Step 5: Commit**

```bash
git add src/agentic_parse/entity_names.py tests/test_entity_names.py
git commit -m "feat: add entity_names extraction prompt and response parser"
```

---

### Task 4: Resolution pure helpers (pre-pass dedup + entity ID + response parser)

**Files:**
- Modify: `src/agentic_parse/entity_names.py`

**Step 1: Write the failing tests**

Add to `tests/test_entity_names.py`:

```python
from agentic_parse.entity_names import (
    _prepass_dedup,
    _entity_name_id,
    _parse_resolution_response,
)

def test_prepass_dedup_collapses_case_and_whitespace() -> None:
    raw = ["JOHN SMITH", "john smith", "John Smith", "  john smith  "]
    result = _prepass_dedup(raw)
    # All map to same canonical
    assert len(set(result.values())) == 1
    assert list(result.values())[0] == "john smith"

def test_prepass_dedup_keeps_distinct_entities() -> None:
    raw = ["John Smith", "Jane Doe"]
    result = _prepass_dedup(raw)
    assert len(set(result.values())) == 2

def test_entity_name_id_is_stable_and_prefixed() -> None:
    eid = _entity_name_id("organization", "safari restaurant")
    assert eid.startswith("ent_")
    assert eid == _entity_name_id("organization", "safari restaurant")

def test_entity_name_id_differs_by_kind() -> None:
    assert _entity_name_id("person", "alex") != _entity_name_id("organization", "alex")

def test_parse_resolution_response_groups_aliases() -> None:
    raw = [
        {"canonical": "safari restaurant", "aliases": ["Safari Restaurant LLC", "Safari Restaurant"]},
    ]
    result = _parse_resolution_response(raw, original_strings=["Safari Restaurant LLC", "Safari Restaurant"])
    assert result["safari restaurant"] == ["Safari Restaurant LLC", "Safari Restaurant"]

def test_parse_resolution_response_self_canonicalizes_missing() -> None:
    raw = [{"canonical": "safari restaurant", "aliases": ["Safari Restaurant"]}]
    result = _parse_resolution_response(raw, original_strings=["Safari Restaurant", "Some Other Org"])
    # "Some Other Org" not in LLM response → self-canonicalized
    assert "some other org" in result
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_entity_names.py -k "dedup or entity_name_id or resolution_response" -v
```

Expected: `ImportError`

**Step 3: Add these helpers to entity_names.py**

Append to `src/agentic_parse/entity_names.py`:

```python
def _prepass_dedup(raw_strings: list[str]) -> dict[str, str]:
    """Map each raw string to its lowercase-stripped canonical form.
    Strings that normalize identically collapse to the same canonical.
    Returns {original_string: canonical_string}.
    """
    result: dict[str, str] = {}
    for s in raw_strings:
        canonical = " ".join(s.strip().lower().split())
        result[s] = canonical
    return result


def _entity_name_id(kind: str, canonical: str) -> str:
    digest = hashlib.sha1(f"{kind}:{canonical}".encode("utf-8")).hexdigest()[:12]
    return f"ent_{digest}"


def _parse_resolution_response(
    raw: list[dict] | None,
    original_strings: list[str],
) -> dict[str, list[str]]:
    """Parse LLM canonicalization response.
    Returns {canonical_name: [alias1, alias2, ...]} for every original string.
    Missing strings are self-canonicalized (lowercase stripped).
    """
    canonical_to_aliases: dict[str, list[str]] = {}
    assigned: set[str] = set()

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            canonical = " ".join(str(item.get("canonical", "")).strip().lower().split())
            if not canonical:
                continue
            aliases = [
                str(a).strip()
                for a in item.get("aliases", [])
                if str(a).strip() and str(a).strip() in original_strings
            ]
            if not aliases:
                continue
            canonical_to_aliases[canonical] = aliases
            assigned.update(aliases)

    # Self-canonicalize anything the LLM missed
    for s in original_strings:
        if s not in assigned:
            canonical = " ".join(s.strip().lower().split())
            canonical_to_aliases.setdefault(canonical, []).append(s)

    return canonical_to_aliases
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_entity_names.py -k "dedup or entity_name_id or resolution_response" -v
```

Expected: 6 PASS

**Step 5: Commit**

```bash
git add src/agentic_parse/entity_names.py tests/test_entity_names.py
git commit -m "feat: add entity_names resolution helpers"
```

---

### Task 5: Phase 1 — per-document extraction worker

**Files:**
- Modify: `src/agentic_parse/entity_names.py`

**Step 1: Write the failing test**

Add to `tests/test_entity_names.py`:

```python
from agentic_parse.config import Settings
from agentic_parse.entity_names import _run_extraction_phase

class StubLLM:
    def __init__(self, response):
        self.response = response
        self.enabled = True
        self.model = "stub"
        self.calls = 0
    def json(self, task, **kwargs):
        self.calls += 1
        return self.response
    def usage_snapshot(self):
        return 0, 0
    def call_event_count(self):
        return 0
    def call_events_since(self, idx):
        return []

def test_extraction_phase_writes_per_doc_json(tmp_path: Path) -> None:
    settings = Settings(workspace=tmp_path, raw_root=tmp_path / "raw")
    settings.ensure_dirs()

    summary_dir = settings.summaries_dir / "doc_abc"
    summary_dir.mkdir(parents=True)
    (summary_dir / "document.summary.txt").write_text("John Smith works at Acme Corp.")

    llm = StubLLM({"persons": ["John Smith"], "organizations": ["Acme Corp"]})

    # Fake DB that returns one doc row
    class FakeConn:
        def execute(self, sql, params=()):
            class FakeCursor:
                def fetchall(self_):
                    if "summary_status" in sql:
                        return [{"document_id": "doc_abc"}]
                    return []
                def fetchone(self_):
                    return None
            return FakeCursor()
        def commit(self): pass

    processed = _run_extraction_phase(settings, FakeConn(), llm)
    out = settings.entity_names_dir / "doc_abc.json"
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["persons"] == ["John Smith"]
    assert data["organizations"] == ["Acme Corp"]
    assert processed == 1
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_entity_names.py::test_extraction_phase_writes_per_doc_json -v
```

Expected: `ImportError: cannot import name '_run_extraction_phase'`

**Step 3: Implement _run_extraction_phase in entity_names.py**

Append to `src/agentic_parse/entity_names.py`:

```python
def _process_doc_worker(settings: Settings, doc_id: str) -> dict:
    """Per-document extraction worker. Stateless — reads files, calls LLM, returns result dict."""
    llm = get_llm_client()
    out_path = settings.entity_names_dir / f"{doc_id}.json"

    # Read summary text
    summary_path = settings.summaries_dir / doc_id / "document.summary.txt"
    summary_text = ""
    if summary_path.exists():
        summary_text = summary_path.read_text(encoding="utf-8", errors="ignore").strip()

    # Retrieve top-3 chunks for additional context (best-effort; skip if no chunks)
    chunks_text = ""
    try:
        from .db import connect  # local import to avoid circular issues in threads
        conn = connect(settings.db_dsn)
        chunks = retrieve_top_k_chunks(
            conn,
            "people names organizations companies employers",
            top_k=3,
            max_chunks=6,
            max_tokens=1500,
            document_id=doc_id,
        )
        conn.close()
        parts = []
        for row in chunks:
            try:
                t = Path(row["text_path"]).read_text(encoding="utf-8", errors="ignore").strip()
                if t:
                    parts.append(t)
            except Exception:
                continue
        chunks_text = "\n\n".join(parts)
    except Exception:
        pass

    if not summary_text and not chunks_text:
        return {"doc_id": doc_id, "skipped": True, "persons": [], "organizations": []}

    user_prompt = _build_extraction_prompt(summary_text=summary_text, chunks_text=chunks_text)

    start = time.perf_counter()
    payload = llm.json(
        task="entity_names_extraction",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "You are a precise entity extractor. Extract only explicitly named people and organizations. "
            "Do not infer, generalize, or include job titles alone. Return strict JSON only."
        ),
        user_prompt=user_prompt,
        max_output_tokens=300,
    )
    duration_ms = (time.perf_counter() - start) * 1000.0

    result = _parse_extraction_response(payload)
    result["document_id"] = doc_id
    write_json(out_path, result)

    return {
        "doc_id": doc_id,
        "skipped": False,
        "persons": result["persons"],
        "organizations": result["organizations"],
        "duration_ms": duration_ms,
    }


def _run_extraction_phase(settings: Settings, conn: Connection, llm) -> int:
    """Phase 1: extract raw persons/orgs per doc in parallel. Returns count of docs processed."""
    docs = conn.execute(
        """
        SELECT document_id
        FROM documents
        WHERE summary_status = 'done' AND status_entity_names != 'done'
        ORDER BY created_at ASC
        """
    ).fetchall()

    if not docs:
        return 0

    processed = 0
    skipped = 0
    progress = tqdm(total=len(docs), desc="entity_names:extract", unit="doc")

    with ThreadPoolExecutor(max_workers=ENTITY_NAMES_MAX_WORKERS) as pool:
        future_to_doc = {
            pool.submit(_process_doc_worker, settings, row["document_id"]): row["document_id"]
            for row in docs
        }
        for future in as_completed(future_to_doc):
            result = future.result()
            doc_id = result["doc_id"]
            if result.get("skipped"):
                skipped += 1
            else:
                processed += 1
            conn.execute(
                "UPDATE documents SET status_entity_names = 'done', updated_at = CURRENT_TIMESTAMP "
                "WHERE document_id = %s",
                (doc_id,),
            )
            progress.set_postfix(processed=processed, skipped=skipped)
            progress.update(1)

    progress.close()
    return processed
```

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_entity_names.py::test_extraction_phase_writes_per_doc_json -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add src/agentic_parse/entity_names.py tests/test_entity_names.py
git commit -m "feat: add entity_names extraction phase with parallel workers"
```

---

### Task 6: Phase 2 — resolution phase and registry builder

**Files:**
- Modify: `src/agentic_parse/entity_names.py`

**Step 1: Write the failing tests**

Add to `tests/test_entity_names.py`:

```python
from agentic_parse.entity_names import _build_registry

def test_build_registry_creates_canonical_entries(tmp_path: Path) -> None:
    settings = Settings(workspace=tmp_path, raw_root=tmp_path / "raw")
    settings.ensure_dirs()

    # Write two per-doc extraction files
    write_doc = lambda doc_id, persons, orgs: (
        settings.entity_names_dir / f"{doc_id}.json"
    ).write_text(json.dumps({"document_id": doc_id, "persons": persons, "organizations": orgs}))

    write_doc("doc_1", ["John Smith", "J. Smith"], ["Safari Restaurant LLC"])
    write_doc("doc_2", ["John Smith"], ["Safari Restaurant"])

    llm = StubLLM([
        # persons resolution batch
        [{"canonical": "john smith", "aliases": ["John Smith", "J. Smith"]}],
        # orgs resolution batch
        [{"canonical": "safari restaurant", "aliases": ["Safari Restaurant LLC", "Safari Restaurant"]}],
    ])
    # Make StubLLM pop from list properly
    class MultiStubLLM:
        def __init__(self, responses):
            self.responses = responses
            self.enabled = True
            self.model = "stub"
        def json(self, task, **kwargs):
            if not self.responses:
                return None
            return self.responses.pop(0)

    registry = _build_registry(settings, MultiStubLLM([
        [{"canonical": "john smith", "aliases": ["John Smith", "J. Smith"]}],
        [{"canonical": "safari restaurant", "aliases": ["Safari Restaurant LLC", "Safari Restaurant"]}],
    ]))

    assert len(registry) == 2
    by_canonical = {e["canonical_name"]: e for e in registry}
    assert "john smith" in by_canonical
    assert "safari restaurant" in by_canonical
    assert set(by_canonical["john smith"]["document_ids"]) == {"doc_1", "doc_2"}
    assert by_canonical["safari restaurant"]["mention_count"] == 2

def test_build_registry_returns_empty_list_when_no_files(tmp_path: Path) -> None:
    settings = Settings(workspace=tmp_path, raw_root=tmp_path / "raw")
    settings.ensure_dirs()
    llm = StubLLM(None)
    result = _build_registry(settings, llm)
    assert result == []
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_entity_names.py -k "build_registry" -v
```

Expected: `ImportError: cannot import name '_build_registry'`

**Step 3: Implement _build_registry and resolution batch call**

Append to `src/agentic_parse/entity_names.py`:

```python
def _resolve_kind_batch(llm, settings: Settings, strings: list[str], kind: str) -> dict[str, list[str]]:
    """Call LLM to canonicalize a batch of raw entity strings of the same kind.
    Returns {canonical_name: [alias1, alias2, ...]}.
    """
    if not strings:
        return {}
    payload = llm.json(
        task="entity_names_resolution",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "You are a canonical entity resolver. Group aliases that refer to the same real-world "
            "entity. Output lowercase canonical names. Do not merge genuinely different entities."
        ),
        user_prompt=(
            f"Here are raw {kind} name strings extracted from documents. Group aliases that refer "
            "to the same entity and choose a canonical lowercase name.\n"
            "Prefer the shortest unambiguous form.\n\n"
            f"RAW STRINGS:\n{json.dumps(strings, ensure_ascii=False)}\n\n"
            "Return JSON array:\n"
            '[{"canonical": "name", "aliases": ["Variant A", "Variant B"]}, ...]\n\n'
            "Every input string must appear in exactly one aliases list. Return JSON only."
        ),
        max_output_tokens=800,
    )
    raw = payload if isinstance(payload, list) else (payload.get("aliases") if isinstance(payload, dict) else None)
    return _parse_resolution_response(raw, original_strings=strings)


def _build_registry(settings: Settings, llm) -> list[dict]:
    """Phase 2: load all per-doc extraction files, resolve aliases, build global registry."""
    # Aggregate raw strings and track which docs each raw string appeared in
    persons_to_docs: dict[str, list[str]] = {}
    orgs_to_docs: dict[str, list[str]] = {}

    for path in sorted(settings.entity_names_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        doc_id = data.get("document_id", path.stem)
        for raw in data.get("persons", []):
            raw = raw.strip()
            if raw:
                persons_to_docs.setdefault(raw, []).append(doc_id)
        for raw in data.get("organizations", []):
            raw = raw.strip()
            if raw:
                orgs_to_docs.setdefault(raw, []).append(doc_id)

    if not persons_to_docs and not orgs_to_docs:
        return []

    def _resolve_pool(raw_to_docs: dict[str, list[str]], kind: str) -> list[dict]:
        # Pre-pass dedup
        deduped = _prepass_dedup(list(raw_to_docs.keys()))
        # Invert: canonical → set of original strings
        canonical_to_raws: dict[str, list[str]] = {}
        for original, canonical in deduped.items():
            canonical_to_raws.setdefault(canonical, []).append(original)

        # Collect unique originals for LLM resolution (skip singletons — already canonical)
        needs_llm = [orig for raws in canonical_to_raws.values() if len(raws) > 1 for orig in raws]
        singletons = [raws[0] for raws in canonical_to_raws.values() if len(raws) == 1]

        # LLM resolution in batches
        llm_resolved: dict[str, list[str]] = {}
        for i in range(0, len(needs_llm), _RESOLUTION_BATCH_SIZE):
            batch = needs_llm[i : i + _RESOLUTION_BATCH_SIZE]
            batch_result = _resolve_kind_batch(llm, settings, batch, kind)
            llm_resolved.update(batch_result)

        # Self-canonicalize singletons
        for s in singletons:
            canonical = " ".join(s.strip().lower().split())
            llm_resolved.setdefault(canonical, []).append(s)

        # Build registry entries
        entries: list[dict] = []
        for canonical, aliases in llm_resolved.items():
            doc_ids: list[str] = []
            mention_count = 0
            for alias in aliases:
                docs = raw_to_docs.get(alias, [])
                doc_ids.extend(docs)
                mention_count += len(docs)
            doc_ids = sorted(set(doc_ids))
            entries.append({
                "entity_id": _entity_name_id(kind, canonical),
                "canonical_name": canonical,
                "kind": kind,
                "aliases": sorted(set(aliases)),
                "document_ids": doc_ids,
                "mention_count": mention_count,
            })
        return entries

    registry: list[dict] = []
    registry.extend(_resolve_pool(persons_to_docs, "person"))
    registry.extend(_resolve_pool(orgs_to_docs, "organization"))
    registry.sort(key=lambda e: (-e["mention_count"], e["canonical_name"]))
    return registry
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_entity_names.py -k "build_registry" -v
```

Expected: 2 PASS

**Step 5: Commit**

```bash
git add src/agentic_parse/entity_names.py tests/test_entity_names.py
git commit -m "feat: add entity_names resolution phase and registry builder"
```

---

### Task 7: Main entry point and registry persistence

**Files:**
- Modify: `src/agentic_parse/entity_names.py`

**Step 1: Write the failing test**

Add to `tests/test_entity_names.py`:

```python
from agentic_parse.entity_names import _write_registry

def test_write_registry_creates_json_and_jsonl(tmp_path: Path) -> None:
    settings = Settings(workspace=tmp_path, raw_root=tmp_path / "raw")
    settings.ensure_dirs()

    registry = [
        {
            "entity_id": "ent_abc",
            "canonical_name": "john smith",
            "kind": "person",
            "aliases": ["John Smith"],
            "document_ids": ["doc_1"],
            "mention_count": 1,
        }
    ]
    _write_registry(settings, registry)

    assert settings.entity_registry_json.exists()
    assert settings.entity_registry_jsonl.exists()

    full = json.loads(settings.entity_registry_json.read_text())
    assert full[0]["canonical_name"] == "john smith"

    lines = settings.entity_registry_jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["entity_id"] == "ent_abc"
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_entity_names.py::test_write_registry_creates_json_and_jsonl -v
```

Expected: `ImportError: cannot import name '_write_registry'`

**Step 3: Implement _write_registry and extract_entity_names entry point**

Append to `src/agentic_parse/entity_names.py`:

```python
def _write_registry(settings: Settings, registry: list[dict]) -> None:
    """Persist the global entity registry to JSON and JSONL."""
    atomic_write_text(settings.entity_registry_json, json.dumps(registry, indent=2))
    lines = [json.dumps(entry, sort_keys=True) for entry in registry]
    atomic_write_text(settings.entity_registry_jsonl, "\n".join(lines) + ("\n" if lines else ""))


def extract_entity_names(settings: Settings, conn: Connection) -> tuple[int, int]:
    """Run both phases of entity name extraction. Returns (entities_written, docs_processed)."""
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    stage_start = time.perf_counter()

    docs_processed = _run_extraction_phase(settings, conn, llm)
    conn.commit()

    registry = _build_registry(settings, llm)
    _write_registry(settings, registry)

    after_in, after_out = llm.usage_snapshot()
    tqdm.write(
        "[entity_names] "
        f"docs_processed={docs_processed} entities_written={len(registry)} "
        f"elapsed_s={(time.perf_counter() - stage_start):.2f}"
    )
    record_stage_metric(
        settings,
        conn,
        "entity_names",
        processed=docs_processed,
        skipped=0,
        failed=0,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
    )
    conn.commit()
    return len(registry), docs_processed
```

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_entity_names.py::test_write_registry_creates_json_and_jsonl -v
```

Expected: PASS

**Step 5: Run all entity_names tests**

```bash
pytest tests/test_entity_names.py -v
```

Expected: All PASS

**Step 6: Commit**

```bash
git add src/agentic_parse/entity_names.py tests/test_entity_names.py
git commit -m "feat: add entity_names entry point and registry persistence"
```

---

### Task 8: CLI integration

**Files:**
- Modify: `src/agentic_parse/cli.py`

**Step 1: Write the failing test**

Add to `tests/test_entity_names.py`:

```python
import subprocess, sys

def test_cli_entity_names_subcommand_exists() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agentic_parse.cli", "entity-names", "--help"],
        capture_output=True, text=True,
    )
    # --help returns exit code 0
    assert result.returncode == 0
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_entity_names.py::test_cli_entity_names_subcommand_exists -v
```

Expected: non-zero returncode (unknown command)

**Step 3: Add entity-names to CLI**

In `src/agentic_parse/cli.py`:

1. Add import at top:
```python
from .entity_names import extract_entity_names
```

2. Add subparser in `_build_parser` after the `entities` subparser line:
```python
sub.add_parser("entity-names", help="Extract and canonicalize named people and organizations.")
```

3. Add handler in `main()` after the `entities` block:
```python
if args.command == "entity-names":
    entities, docs = extract_entity_names(settings, conn)
    print(f"entities_written={entities} docs_processed={docs}")
    return
```

4. In the `all` command block, add after `summarize`:
```python
en_entities, en_docs = extract_entity_names(settings, conn)
```

And include in the final print:
```python
f"entity_names_written={en_entities}",
```

5. In `_status`, add:
```python
done_entity_names = conn.execute("SELECT COUNT(*) FROM documents WHERE status_entity_names = 'done'").fetchone()[0]
```
And include `f"entity_names_done={done_entity_names}"` in the return string.

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_entity_names.py::test_cli_entity_names_subcommand_exists -v
```

Expected: PASS

**Step 5: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All PASS

**Step 6: Commit**

```bash
git add src/agentic_parse/cli.py tests/test_entity_names.py
git commit -m "feat: wire entity-names into CLI and all-pipeline command"
```
