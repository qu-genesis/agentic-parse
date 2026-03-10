# Entity Names Extraction — Design Doc

**Date:** 2026-03-10
**Branch:** document-catalogue-panel
**Status:** Approved

---

## Goal

Extract named people and organizations from every document in the corpus, normalize them to
lowercase canonical forms, resolve small inconsistencies across the corpus (e.g. "Safari
Restaurant LLC" == "Safari Restaurant"), and expose the results for per-document display and
cross-document search.

**Out of scope:** Typed relationships between entities (handled separately by `entities.py`).

---

## Architecture Overview

A two-phase pipeline stage `extract_entity_names`, implemented in a new `entity_names.py`
module. Runs after the `summarize` stage.

```
[summarize stage done]
        │
        ▼
Phase 1 — Extraction (parallel, 1 LLM call/doc)
  For each doc: summary text + top-3 chunks → {persons: [], organizations: []}
  Stored per-doc in: outputs/entity_names/{doc_id}.json
        │
        ▼
Phase 2 — Resolution (serial batches, ~N/100 LLM calls)
  Collect all unique raw strings → batch canonicalize → global registry
  Output: outputs/entity_registry.json
          outputs/entity_registry.jsonl
```

The `documents` table gets a new status column `status_entity_names` so the stage is
resumable and idempotent.

---

## Data Model

### Per-document extraction — `outputs/entity_names/{doc_id}.json`

```json
{
  "document_id": "abc123",
  "persons": ["John Smith", "J. Smith", "Mary O'Brien"],
  "organizations": ["Safari Restaurant LLC", "City of Portland", "Safari Restaurant"]
}
```

Raw strings only — no normalization at this stage. All variants preserved to maximize alias
signal for the resolution phase.

### Global entity registry — `outputs/entity_registry.json` + `.jsonl`

One entry per canonical entity:

```json
{
  "entity_id": "ent_a3f9c2",
  "canonical_name": "safari restaurant",
  "kind": "organization",
  "aliases": ["Safari Restaurant LLC", "Safari Restaurant", "safari restaurant llc"],
  "document_ids": ["abc123", "def456"],
  "mention_count": 4
}
```

- `entity_id`: `"ent_" + sha1(f"{kind}:{canonical_name}")[:12]` — stable across reruns
- `canonical_name`: always lowercase
- Legal suffixes (`LLC`, `Inc.`, `Corp.`, `Ltd.`) stripped only when a shorter alias
  co-occurs in the same resolution batch — never blindly stripped
- Registry is **fully rebuilt** each pipeline run from per-doc files — no incremental merge

---

## Phase 1 — Extraction

### Context per document

- Document summary text (from `summaries/{doc_id}/document.summary.txt`)
- Top-3 chunks retrieved with query `"people names organizations companies employers"`
- Total context capped at ~4,000 chars

### LLM prompt

```
System:
  You are a precise entity extractor. Extract only explicitly named people and organizations.
  Do not infer, generalize, or include job titles alone. Return strict JSON only.

User:
  Extract all named persons and organizations from the following document context.
  Return {"persons": ["Full Name", ...], "organizations": ["Org Name", ...]}
  Include every variant or abbreviation you see — do not normalize.
  If none found, return empty lists.

  CONTEXT:
  {summary_text}

  CHUNKS:
  {top_3_chunks}

  Return JSON only.
```

`max_output_tokens: 300`

### Parallelism

`ThreadPoolExecutor` with `ENTITY_NAMES_MAX_WORKERS` workers (default: 16). Each worker is
stateless — writes result to `outputs/entity_names/{doc_id}.json`, returns nothing to pool.

### Skipping

If `outputs/entity_names/{doc_id}.json` exists and `status_entity_names = 'done'`, skip.

---

## Phase 2 — Resolution

### Aggregation

Load all per-doc JSON files. Build two pools: `persons_pool` and `orgs_pool`. Track which
`document_ids` each raw string appeared in.

### Pre-pass deduplication (free)

Case-insensitive exact match + strip punctuation/whitespace collapses obvious duplicates
before any LLM call (e.g. `JOHN SMITH` → `john smith`).

### LLM canonicalization (per batch of ~100 strings, same kind)

```
System:
  You are a canonical entity resolver. Group aliases that refer to the same real-world
  entity. Output lowercase canonical names. Do not merge entities that are genuinely
  different people or organizations.

User:
  Here are raw {kind} name strings extracted from documents. Group aliases that refer to
  the same entity and choose a canonical lowercase name.
  Prefer the shortest unambiguous form ("safari restaurant" not "safari restaurant llc")
  unless that would cause confusion.

  RAW STRINGS:
  ["Safari Restaurant LLC", "Safari Restaurant", ...]

  Return JSON array:
  [{"canonical": "safari restaurant", "aliases": ["Safari Restaurant LLC", ...]}, ...]

  Every input string must appear in exactly one aliases list. Return JSON only.
```

### Fallback

Any raw string omitted from the LLM response is self-canonicalized: lowercased + stripped.

### Entity ID assignment

After resolution: `entity_id = "ent_" + sha1(f"{kind}:{canonical_name}")[:12]`. Attach
`document_ids` by inverting the raw-string → doc mapping from the aggregation step.

### Cost estimate

500 docs × ~10 unique entities → ~5,000 raw strings → ~200 unique after pre-dedup →
**~2 LLM resolution calls total**. Extraction: ~500 LLM calls (1/doc, parallelized).

---

## Storage and Integration

### New module

`src/agentic_parse/entity_names.py` — self-contained. No changes to `entities.py` or
`summarize.py`.

### DB change

```sql
ALTER TABLE documents
  ADD COLUMN status_entity_names TEXT NOT NULL DEFAULT 'pending';
```

### `config.py` additions

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

### Pipeline hook

`entity_names.extract_entity_names(settings, conn)` called from the main pipeline after
`summarize`. Returns `(entities_written, docs_processed)`.

### Viewer / search integration

- Per-doc display: reads `outputs/entity_names/{doc_id}.json`
- Cross-document search: reads `outputs/entity_registry.jsonl`
- No DB entity table — file-based, consistent with existing `outputs/entities/` pattern
