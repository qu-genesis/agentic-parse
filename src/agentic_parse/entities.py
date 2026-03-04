from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_stage_metric
from .utils import append_jsonl, write_json


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
            "Extract entities and relationships from text with evidence. "
            "Return strict JSON with keys: entities, relationships. "
            "Each entity: value, kind, confidence. "
            "Each relationship: subject_value, predicate, object_value, confidence, evidence_excerpt. "
            "Use only information explicitly in the input text. "
            "Return JSON only, no markdown."
        ),
        user_prompt=(
            "Text chunk:\n"
            f"{text}\n\n"
            "Output schema:\n"
            "{\"entities\":[{\"value\":\"...\",\"kind\":\"...\",\"confidence\":0.0}],"
            "\"relationships\":[{\"subject_value\":\"...\",\"predicate\":\"...\","
            "\"object_value\":\"...\",\"confidence\":0.0,\"evidence_excerpt\":\"...\"}]}"
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


def extract_entities(settings: Settings, conn: Connection) -> tuple[int, int]:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.document_id, c.page_number, c.text_path,
               p.timestamp_start_ms, p.timestamp_end_ms
        FROM chunks c
        JOIN documents d ON d.document_id = c.document_id
        LEFT JOIN pages p ON p.page_id = c.page_id
        WHERE d.status_entities != 'done'
        ORDER BY c.document_id, c.page_number, c.chunk_index
        """
    ).fetchall()

    if not rows:
        record_stage_metric(settings, conn, "entities", processed=0, skipped=0, failed=0)
        conn.commit()
        return 0, 0

    entity_updates: dict[str, dict] = {}
    relationship_writes = 0
    touched_docs: set[str] = set()
    skipped = 0

    for row in rows:
        text_path = Path(row["text_path"])
        if not text_path.exists():
            skipped += 1
            continue
        text = text_path.read_text(encoding="utf-8")
        llm_result = _extract_entities_with_llm(settings, text)
        if llm_result is None:
            found = _extract_entities_regex(text)
            llm_relationships: list[dict] = []
        else:
            llm_entities, llm_relationships = llm_result
            found = []
            for ent in llm_entities:
                value = str(ent.get("value", "")).strip()
                kind = str(ent.get("kind", "unknown")).strip().lower() or "unknown"
                if not value:
                    continue
                pos = text.lower().find(value.lower())
                start = pos if pos >= 0 else 0
                end = start + len(value)
                found.append({"value": value, "kind": kind, "start": start, "end": end})
        if not found:
            skipped += 1
            continue

        unique_entities_in_chunk = []
        seen_chunk_ids: set[str] = set()
        for ent in found:
            entity_id = _entity_id(ent["value"], ent["kind"])
            if entity_id not in seen_chunk_ids:
                unique_entities_in_chunk.append((entity_id, ent))
                seen_chunk_ids.add(entity_id)

            card_path = settings.entities_dir / f"{entity_id}.json"
            card = entity_updates.get(entity_id)
            if card is None:
                card = _load_entity_card(card_path) or {
                    "entity_id": entity_id,
                    "kind": ent["kind"],
                    "canonical_name": ent["value"],
                    "aliases": [],
                    "mentions": 0,
                    "evidence": [],
                    "confidence": "medium",
                }
            if ent["value"] not in card["aliases"]:
                card["aliases"].append(ent["value"])
            card["mentions"] += 1
            card["evidence"].append(
                {
                    "document_id": row["document_id"],
                    "page_number": row["page_number"],
                    "timestamp_start_ms": row["timestamp_start_ms"],
                    "timestamp_end_ms": row["timestamp_end_ms"],
                    "excerpt": _evidence_excerpt(text, ent["start"], ent["end"]),
                }
            )
            card["evidence"] = card["evidence"][-20:]
            entity_updates[entity_id] = card

        alias_to_id = {ent["value"].strip().lower(): entity_id for entity_id, ent in unique_entities_in_chunk}

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
                        start = min(subj_pos, obj_pos)
                        end = max(subj_pos + len(subj_value), obj_pos + len(obj_value))
                        evidence = _evidence_excerpt(text, start, end)
                    else:
                        evidence = text[:180]
                edge_id = _edge_id(
                    subj_id,
                    predicate,
                    obj_id,
                    row["document_id"],
                    row["page_number"],
                    row["timestamp_start_ms"],
                    row["timestamp_end_ms"],
                )
                if _upsert_relationship(
                    settings,
                    conn,
                    edge_id=edge_id,
                    subject_entity_id=subj_id,
                    predicate=predicate,
                    object_entity_id=obj_id,
                    document_id=row["document_id"],
                    page_number=row["page_number"],
                    timestamp_start_ms=row["timestamp_start_ms"],
                    timestamp_end_ms=row["timestamp_end_ms"],
                    evidence_excerpt=evidence,
                    confidence=confidence,
                ):
                    relationship_writes += 1
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
                    edge_id = _edge_id(
                        subj_id,
                        "co_mentioned",
                        obj_id,
                        row["document_id"],
                        row["page_number"],
                        row["timestamp_start_ms"],
                        row["timestamp_end_ms"],
                    )
                    if _upsert_relationship(
                        settings,
                        conn,
                        edge_id=edge_id,
                        subject_entity_id=subj_id,
                        predicate="co_mentioned",
                        object_entity_id=obj_id,
                        document_id=row["document_id"],
                        page_number=row["page_number"],
                        timestamp_start_ms=row["timestamp_start_ms"],
                        timestamp_end_ms=row["timestamp_end_ms"],
                        evidence_excerpt=evidence,
                        confidence=0.5,
                    ):
                        relationship_writes += 1

        touched_docs.add(row["document_id"])

    for entity_id, card in entity_updates.items():
        write_json(settings.entities_dir / f"{entity_id}.json", card)

    for doc_id in touched_docs:
        conn.execute(
            "UPDATE documents SET status_entities = 'done', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (doc_id,),
        )

    after_in, after_out = llm.usage_snapshot()
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
        contexts.append(
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "page_number": row["page_number"],
                "text": text,
            }
        )

    prompt = {
        "query": query,
        "contexts": contexts,
        "instructions": "Answer with cited chunk_ids only from provided contexts.",
    }
    answer = llm.text(
        task="retrieval_first_query",
        cache_dir=settings.llm_cache_dir,
        system_prompt="You are a retrieval-grounded extractor. Use only provided contexts.",
        user_prompt=json.dumps(prompt, ensure_ascii=True),
        max_output_tokens=500,
    )
    if answer is None:
        answer = "No model response."

    result = {
        "query": query,
        "chunks_used": len(contexts),
        "chunk_ids": [c["chunk_id"] for c in contexts],
        "answer": answer,
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
