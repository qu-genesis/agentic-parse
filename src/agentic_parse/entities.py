from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import hashlib
import json
import os
import re
import time

from tqdm import tqdm

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_costly_call, record_stage_metric
from .utils import append_jsonl, write_json

ENTITIES_MAX_WORKERS = int(os.getenv("ENTITIES_MAX_WORKERS", "16"))


NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")


def _entity_id(value: str, kind: str) -> str:
    norm = re.sub(r"\s+", " ", value.strip().lower())
    digest = hashlib.sha1(f"{kind}:{norm}".encode("utf-8")).hexdigest()[:12]
    return f"ent_{digest}"


def _extract_entities_regex(text: str) -> list[dict]:
    entities = []
    for match in NAME_RE.finditer(text):
        val = match.group(1).strip()
        if len(val) < 3:
            continue
        entities.append({"value": val, "kind": "name", "start": match.start(), "end": match.end()})
    for match in EMAIL_RE.finditer(text):
        val = match.group(1).strip()
        entities.append({"value": val, "kind": "email", "start": match.start(), "end": match.end()})
    for match in DATE_RE.finditer(text):
        val = match.group(1).strip()
        entities.append({"value": val, "kind": "date", "start": match.start(), "end": match.end()})
    return entities


def _evidence_excerpt(text: str, start: int, end: int, window: int = 60) -> str:
    s = max(0, start - window)
    e = min(len(text), end + window)
    return " ".join(text[s:e].split())


def _edge_id(subj: str, pred: str, obj: str, doc: str, page: int | None, ts_start: int | None, ts_end: int | None) -> str:
    raw = f"{subj}|{pred}|{obj}|{doc}|{page}|{ts_start}|{ts_end}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_entity_card(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_confidence(value: object, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _extract_entities_with_llm(settings: Settings, text: str) -> tuple[list[dict], list[dict]] | None:
    llm = get_llm_client()
    payload = llm.json(
        task="chunk_entities_relationships",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "Extract entities and relationships from the input text with evidence.\n\n"
            "Rules:\n"
            "- Use only information explicitly present in the text.\n"
            "- Return strict JSON only with keys: 'entities', 'relationships'.\n\n"
            "Controlled vocabularies (use exactly as listed):\n"
            "entity.kind MUST be one of:\n"
            '["person","organization","address","email","phone","date","money",'
            '"document_id","account_id","invoice_id","payment_id","location","url","other"]\n\n'
            "relationship.predicate MUST be one of:\n"
            '["pays","paid_by","billed_to","ships_to","issued_by","address_of","contact_of",'
            '"employed_by","references","total_amount","has_line_item","dated","signed_by","part_of","other"]\n\n'
            "confidence is 0.0–1.0:\n"
            "  0.9–1.0 = explicitly stated and unambiguous\n"
            "  0.6–0.89 = explicit but context is partial or format unclear\n"
            "  0.3–0.59 = weak signal; only include if still explicitly stated\n\n"
            "evidence_excerpt MUST be a direct quote from the text, max 160 characters.\n"
            "Return JSON only, no markdown."
        ),
        user_prompt=(
            "Extract entities and relationships from this text chunk.\n\n"
            f"Text:\n{text}\n\n"
            "Output must match this schema exactly:\n"
            "{\"entities\":[{\"value\":\"...\",\"kind\":\"...\",\"confidence\":0.0}],"
            "\"relationships\":[{\"subject_value\":\"...\",\"predicate\":\"...\","
            "\"object_value\":\"...\",\"confidence\":0.0,\"evidence_excerpt\":\"...\"}]}\n\n"
            "Return JSON only."
        ),
        max_output_tokens=1400,
    )
    if not payload:
        return None

    entities = payload.get("entities", [])
    relationships = payload.get("relationships", [])
    if not isinstance(entities, list) or not isinstance(relationships, list):
        return None
    return entities, relationships


def _upsert_relationship(
    settings: Settings,
    conn: Connection,
    *,
    edge_id: str,
    subject_entity_id: str,
    predicate: str,
    object_entity_id: str,
    document_id: str,
    page_number: int | None,
    timestamp_start_ms: int | None,
    timestamp_end_ms: int | None,
    evidence_excerpt: str,
    confidence: float,
) -> bool:
    exists = conn.execute("SELECT 1 FROM relationships WHERE edge_id = %s", (edge_id,)).fetchone()
    if exists:
        return False

    conn.execute(
        """
        INSERT INTO relationships (
            edge_id, subject_entity_id, predicate, object_entity_id,
            document_id, page_number, timestamp_start_ms, timestamp_end_ms,
            evidence_excerpt, confidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            edge_id,
            subject_entity_id,
            predicate,
            object_entity_id,
            document_id,
            page_number,
            timestamp_start_ms,
            timestamp_end_ms,
            evidence_excerpt,
            confidence,
        ),
    )
    append_jsonl(
        settings.relationships_jsonl,
        {
            "edge_id": edge_id,
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
            "object_entity_id": object_entity_id,
            "document_id": document_id,
            "page_number": page_number,
            "timestamp_start_ms": timestamp_start_ms,
            "timestamp_end_ms": timestamp_end_ms,
            "evidence_excerpt": evidence_excerpt,
            "confidence": confidence,
        },
    )
    return True


def _process_chunk_worker(settings: Settings, row: dict) -> dict:
    """Process a single chunk and return entity deltas + relationship dicts.

    Designed to run in a thread. No DB or shared-dict writes — returns plain
    data that the caller merges serially.
    """
    from .llm import get_llm_client  # import here to avoid circular issues in threads

    llm = get_llm_client()
    doc_id = row["document_id"]
    chunk_id = row["chunk_id"]
    base = dict(
        doc_id=doc_id,
        chunk_id=chunk_id,
        entity_deltas={},   # entity_id → delta dict
        relationships=[],   # list of ready-to-insert relationship dicts
        entity_count=0,
        skipped=False,
        extraction_method="unknown",
        duration_ms=0.0,
        llm_event_start=0,
        llm_event_end=0,
    )

    text_path = Path(row["text_path"])
    if not text_path.exists():
        base["skipped"] = True
        base["extraction_method"] = "missing_file"
        return base

    chunk_start = time.perf_counter()
    text = text_path.read_text(encoding="utf-8")

    llm_event_start = llm.call_event_count()
    llm_result = _extract_entities_with_llm(settings, text)
    llm_event_end = llm.call_event_count()
    base["llm_event_start"] = llm_event_start
    base["llm_event_end"] = llm_event_end

    if llm_result is None:
        found = _extract_entities_regex(text)
        llm_relationships: list[dict] = []
        extraction_method = "regex_fallback"
    else:
        llm_entities, llm_relationships = llm_result
        found = []
        extraction_method = "llm_json"
        for ent in llm_entities:
            value = str(ent.get("value", "")).strip()
            kind = str(ent.get("kind", "unknown")).strip().lower() or "unknown"
            if not value:
                continue
            pos = text.lower().find(value.lower())
            start = pos if pos >= 0 else 0
            end = start + len(value)
            found.append({"value": value, "kind": kind, "start": start, "end": end})

    base["extraction_method"] = extraction_method

    if not found:
        base["skipped"] = True
        base["duration_ms"] = (time.perf_counter() - chunk_start) * 1000.0
        return base

    # Build per-entity deltas (mentions, aliases, evidence increments only).
    # The caller loads the base card from disk and applies these deltas.
    entity_deltas: dict[str, dict] = {}
    unique_entities_in_chunk: list[tuple[str, dict]] = []
    seen_ids: set[str] = set()

    for ent in found:
        entity_id = _entity_id(ent["value"], ent["kind"])
        if entity_id not in seen_ids:
            unique_entities_in_chunk.append((entity_id, ent))
            seen_ids.add(entity_id)

        delta = entity_deltas.setdefault(
            entity_id,
            {
                "entity_id": entity_id,
                "kind": ent["kind"],
                "canonical_name": ent["value"],
                "mentions_delta": 0,
                "new_aliases": [],
                "new_evidence": [],
            },
        )
        delta["mentions_delta"] += 1
        if ent["value"] not in delta["new_aliases"]:
            delta["new_aliases"].append(ent["value"])
        delta["new_evidence"].append(
            {
                "document_id": row["document_id"],
                "page_number": row["page_number"],
                "timestamp_start_ms": row["timestamp_start_ms"],
                "timestamp_end_ms": row["timestamp_end_ms"],
                "excerpt": _evidence_excerpt(text, ent["start"], ent["end"]),
            }
        )

    alias_to_id = {
        ent["value"].strip().lower(): entity_id
        for entity_id, ent in unique_entities_in_chunk
    }

    # Build relationship dicts (ready for _upsert_relationship).
    relationships: list[dict] = []
    if llm_relationships:
        for rel in llm_relationships:
            subj_value = str(rel.get("subject_value", "")).strip().lower()
            obj_value = str(rel.get("object_value", "")).strip().lower()
            predicate = str(rel.get("predicate", "related_to")).strip() or "related_to"
            evidence = str(rel.get("evidence_excerpt", "")).strip()
            confidence = _safe_confidence(rel.get("confidence", 0.5))
            subj_id = alias_to_id.get(subj_value)
            obj_id = alias_to_id.get(obj_value)
            if not subj_id or not obj_id or subj_id == obj_id:
                continue
            if not evidence:
                subj_pos = text.lower().find(subj_value)
                obj_pos = text.lower().find(obj_value)
                if subj_pos >= 0 and obj_pos >= 0:
                    s = min(subj_pos, obj_pos)
                    e = max(subj_pos + len(subj_value), obj_pos + len(obj_value))
                    evidence = _evidence_excerpt(text, s, e)
                else:
                    evidence = text[:180]
            relationships.append(
                dict(
                    edge_id=_edge_id(
                        subj_id, predicate, obj_id,
                        row["document_id"], row["page_number"],
                        row["timestamp_start_ms"], row["timestamp_end_ms"],
                    ),
                    subject_entity_id=subj_id,
                    predicate=predicate,
                    object_entity_id=obj_id,
                    document_id=row["document_id"],
                    page_number=row["page_number"],
                    timestamp_start_ms=row["timestamp_start_ms"],
                    timestamp_end_ms=row["timestamp_end_ms"],
                    evidence_excerpt=evidence,
                    confidence=confidence,
                )
            )
    else:
        for i in range(len(unique_entities_in_chunk)):
            for j in range(i + 1, len(unique_entities_in_chunk)):
                subj_id = unique_entities_in_chunk[i][0]
                obj_id = unique_entities_in_chunk[j][0]
                evidence = _evidence_excerpt(
                    text,
                    unique_entities_in_chunk[i][1]["start"],
                    unique_entities_in_chunk[j][1]["end"],
                )
                relationships.append(
                    dict(
                        edge_id=_edge_id(
                            subj_id, "co_mentioned", obj_id,
                            row["document_id"], row["page_number"],
                            row["timestamp_start_ms"], row["timestamp_end_ms"],
                        ),
                        subject_entity_id=subj_id,
                        predicate="co_mentioned",
                        object_entity_id=obj_id,
                        document_id=row["document_id"],
                        page_number=row["page_number"],
                        timestamp_start_ms=row["timestamp_start_ms"],
                        timestamp_end_ms=row["timestamp_end_ms"],
                        evidence_excerpt=evidence,
                        confidence=0.5,
                    )
                )

    base["entity_deltas"] = entity_deltas
    base["relationships"] = relationships
    base["entity_count"] = len(unique_entities_in_chunk)
    base["duration_ms"] = (time.perf_counter() - chunk_start) * 1000.0
    return base


def _merge_entity_deltas(
    settings: Settings,
    entity_updates: dict[str, dict],
    entity_deltas: dict[str, dict],
) -> None:
    """Apply per-chunk deltas onto entity_updates, loading from disk on first encounter."""
    for entity_id, delta in entity_deltas.items():
        card = entity_updates.get(entity_id)
        if card is None:
            card_path = settings.entities_dir / f"{entity_id}.json"
            card = _load_entity_card(card_path) or {
                "entity_id": entity_id,
                "kind": delta["kind"],
                "canonical_name": delta["canonical_name"],
                "aliases": [],
                "mentions": 0,
                "evidence": [],
                "confidence": "medium",
            }
        card["mentions"] += delta["mentions_delta"]
        for alias in delta["new_aliases"]:
            if alias not in card["aliases"]:
                card["aliases"].append(alias)
        card["evidence"].extend(delta["new_evidence"])
        card["evidence"] = card["evidence"][-20:]
        entity_updates[entity_id] = card


def extract_entities(settings: Settings, conn: Connection) -> tuple[int, int]:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    llm_event_start = llm.call_event_count()
    stage_start = time.perf_counter()
    total_docs = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE status_entities != 'done'"
    ).fetchone()[0]

    if not total_docs:
        record_stage_metric(settings, conn, "entities", processed=0, skipped=0, failed=0)
        conn.commit()
        return 0, 0

    row_cursor = conn.execute(
        """
        SELECT c.chunk_id, c.document_id, c.page_number, c.text_path,
               p.timestamp_start_ms, p.timestamp_end_ms
        FROM chunks c
        JOIN documents d ON d.document_id = c.document_id
        LEFT JOIN pages p ON p.page_id = c.page_id
        WHERE d.status_entities != 'done'
        ORDER BY c.document_id, c.page_number, c.chunk_index
        """
    )

    entity_updates: dict[str, dict] = {}
    relationship_writes = 0
    touched_docs: set[str] = set()
    skipped = 0
    parsed_docs: set[str] = set()
    verbose_chunk_logs = os.getenv("ENTITIES_PROGRESS_VERBOSE", "0") == "1"
    progress = tqdm(total=total_docs, desc="entities", unit="doc")
    doc_chunk_counts: dict[str, int] = {}

    while True:
        batch = row_cursor.fetchmany(256)
        if not batch:
            break

        # Fan out chunk processing across a thread pool. Each worker makes its
        # own LLM call and returns plain data — no shared state is mutated.
        with ThreadPoolExecutor(max_workers=ENTITIES_MAX_WORKERS) as pool:
            future_to_row = {
                pool.submit(_process_chunk_worker, settings, row): row
                for row in batch
            }

            for future in as_completed(future_to_row):
                result = future.result()
                doc_id = result["doc_id"]

                doc_chunk_counts[doc_id] = doc_chunk_counts.get(doc_id, 0) + 1
                if doc_id not in parsed_docs:
                    parsed_docs.add(doc_id)
                    progress.update(1)

                # Record per-LLM-event telemetry (event indices may overlap across
                # concurrent threads in verbose mode, but are accurate enough).
                chunk_llm_events = llm.call_events_since(result["llm_event_start"])
                for event in chunk_llm_events:
                    if verbose_chunk_logs:
                        tqdm.write(
                            "[entities][llm] "
                            f"doc={doc_id} chunk={result['chunk_id']} "
                            f"method={event['method']} task={event['task']} "
                            f"latency_ms={event['duration_ms']:.2f} "
                            f"cache_hit={event['cache_hit']} success={event['success']}"
                        )
                    record_costly_call(
                        settings,
                        conn,
                        stage="entities",
                        step=str(event["task"]),
                        location="entities.extract_entities",
                        call_type="llm_call",
                        duration_ms=float(event["duration_ms"]),
                        document_id=doc_id,
                        chunk_id=result["chunk_id"],
                        provider="openai",
                        model_version=llm.model,
                        cache_hit=bool(event["cache_hit"]),
                        success=bool(event["success"]),
                        metadata={"method": event["method"]},
                    )

                if result["skipped"]:
                    skipped += 1
                    record_costly_call(
                        settings,
                        conn,
                        stage="entities",
                        step="chunk_extract",
                        location="entities.extract_entities",
                        call_type="doc_operation",
                        duration_ms=result["duration_ms"],
                        document_id=doc_id,
                        chunk_id=result["chunk_id"],
                        metadata={"entities_found": 0, "method": result["extraction_method"]},
                        success=True,
                    )
                    if verbose_chunk_logs:
                        tqdm.write(
                            "[entities] "
                            f"doc={doc_id} chunk={result['chunk_id']} entities=0 "
                            f"method={result['extraction_method']} "
                            f"chunk_elapsed_ms={result['duration_ms']:.1f}"
                        )
                    progress.set_postfix(processed=len(entity_updates), skipped=skipped)
                    continue

                # Merge entity deltas into the shared entity_updates dict (serial, safe).
                _merge_entity_deltas(settings, entity_updates, result["entity_deltas"])

                # Write relationships to DB (serial, safe).
                for rel in result["relationships"]:
                    if _upsert_relationship(settings, conn, **rel):
                        relationship_writes += 1

                touched_docs.add(doc_id)
                record_costly_call(
                    settings,
                    conn,
                    stage="entities",
                    step="chunk_extract",
                    location="entities.extract_entities",
                    call_type="doc_operation",
                    duration_ms=result["duration_ms"],
                    document_id=doc_id,
                    chunk_id=result["chunk_id"],
                    metadata={
                        "entities_found": result["entity_count"],
                        "method": result["extraction_method"],
                    },
                    success=True,
                )
                if verbose_chunk_logs:
                    tqdm.write(
                        "[entities] "
                        f"doc={doc_id} chunk={result['chunk_id']} "
                        f"entities={result['entity_count']} "
                        f"relationships_total={relationship_writes} "
                        f"method={result['extraction_method']} "
                        f"chunk_elapsed_ms={result['duration_ms']:.1f}"
                    )
                progress.set_postfix(processed=len(entity_updates), skipped=skipped)

    progress.close()

    for entity_id, card in entity_updates.items():
        write_json(settings.entities_dir / f"{entity_id}.json", card)

    for doc_id in touched_docs:
        conn.execute(
            "UPDATE documents SET status_entities = 'done', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (doc_id,),
        )
        tqdm.write(
            "[entities] "
            f"doc={doc_id} chunks={doc_chunk_counts.get(doc_id, 0)} status_entities=done"
        )

    after_in, after_out = llm.usage_snapshot()
    llm_events = llm.call_events_since(llm_event_start)
    tqdm.write(
        "[entities] "
        f"documents={total_docs} llm_calls={len(llm_events)} "
        f"llm_total_ms={sum(float(e['duration_ms']) for e in llm_events):.2f} "
        f"elapsed_s={(time.perf_counter() - stage_start):.2f}"
    )
    record_stage_metric(
        settings,
        conn,
        "entities",
        processed=len(entity_updates),
        skipped=skipped,
        failed=0,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
    )
    conn.commit()
    return len(entity_updates), relationship_writes


def extract_for_query(
    settings: Settings,
    conn: Connection,
    *,
    query: str,
    top_k: int,
    max_chunks: int,
    max_tokens: int,
) -> dict:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    chunks = retrieve_top_k_chunks(conn, query, top_k=top_k, max_chunks=max_chunks, max_tokens=max_tokens)
    if not chunks:
        result = {"query": query, "chunks_used": 0, "answer": "No relevant chunks found."}
        record_stage_metric(settings, conn, "extract_query", processed=0, skipped=0, failed=0)
        conn.commit()
        return result

    contexts = []
    for row in chunks:
        text = Path(row["text_path"]).read_text(encoding="utf-8", errors="ignore")
        contexts.append({"chunk_id": row["chunk_id"], "text": text})

    user_prompt_payload = {"query": query, "contexts": contexts}
    rag_response = llm.json(
        task="retrieval_first_query",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "You are a retrieval-grounded question answerer.\n\n"
            "Rules:\n"
            "- You MUST use only the provided contexts.\n"
            "- Every claim in the answer MUST be supported by one or more chunk_ids from the contexts.\n"
            "- If the contexts do not contain enough evidence, say so in 'uncertainties' "
            "and cite the closest relevant chunk_ids.\n\n"
            "Return strict JSON only with this schema:\n"
            '{"answer": "string", "citations": ["chunk_id", "..."], "uncertainties": ["string", "..."]}'
        ),
        user_prompt=(
            json.dumps(user_prompt_payload, ensure_ascii=True) + "\n\n"
            "Answer the query using only the contexts above. "
            "Cite only chunk_id values found in the contexts. Return JSON only."
        ),
        max_output_tokens=600,
    )

    if rag_response:
        answer = rag_response.get("answer", "No model response.")
        citations = rag_response.get("citations", [])
        uncertainties = rag_response.get("uncertainties", [])
    else:
        answer = "No model response."
        citations = []
        uncertainties = []

    result = {
        "query": query,
        "chunks_used": len(contexts),
        "chunk_ids": [c["chunk_id"] for c in contexts],
        "answer": answer,
        "citations": citations,
        "uncertainties": uncertainties,
    }

    after_in, after_out = llm.usage_snapshot()
    record_stage_metric(
        settings,
        conn,
        "extract_query",
        processed=1,
        skipped=0,
        failed=0,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
        metadata={"top_k": top_k, "max_chunks": max_chunks, "max_tokens": max_tokens},
    )
    conn.commit()
    return result
