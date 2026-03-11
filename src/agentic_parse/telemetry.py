from __future__ import annotations

import hashlib
import json
import os

from .config import Settings
from .db import Connection
from .utils import append_jsonl

_COSTLY_CALL_MIN_MS = float(os.getenv("COSTLY_CALL_MIN_MS", "25"))


def record_stage_metric(
    settings: Settings,
    conn: Connection,
    stage: str,
    processed: int,
    skipped: int,
    failed: int,
    token_input: int = 0,
    token_output: int = 0,
    metadata: dict | None = None,
) -> None:
    payload = {
        "stage": stage,
        "processed_count": processed,
        "skipped_count": skipped,
        "failed_count": failed,
        "token_input": token_input,
        "token_output": token_output,
        "metadata": metadata or {},
    }
    conn.execute(
        """
        INSERT INTO stage_metrics (
            stage, processed_count, skipped_count, failed_count,
            token_input, token_output, metadata_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            stage,
            processed,
            skipped,
            failed,
            token_input,
            token_output,
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    append_jsonl(settings.stage_metrics_jsonl, payload)


def record_costly_call(
    settings: Settings,
    conn: Connection,
    *,
    stage: str,
    step: str,
    location: str,
    call_type: str,
    duration_ms: float,
    document_id: str | None = None,
    page_id: str | None = None,
    chunk_id: str | None = None,
    provider: str | None = None,
    model_version: str | None = None,
    token_input: int = 0,
    token_output: int = 0,
    cache_hit: bool | None = None,
    success: bool | None = None,
    metadata: dict | None = None,
) -> None:
    payload = {
        "stage": stage,
        "step": step,
        "location": location,
        "call_type": call_type,
        "document_id": document_id,
        "page_id": page_id,
        "chunk_id": chunk_id,
        "provider": provider,
        "model_version": model_version,
        "duration_ms": round(max(0.0, float(duration_ms)), 2),
        "token_input": int(max(0, token_input)),
        "token_output": int(max(0, token_output)),
        "cache_hit": cache_hit,
        "success": success,
        "metadata": metadata or {},
    }
    conn.execute(
        """
        INSERT INTO costly_calls (
            stage, step, location, call_type, document_id, page_id, chunk_id,
            provider, model_version, duration_ms, token_input, token_output,
            cache_hit, success, metadata_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            stage,
            step,
            location,
            call_type,
            document_id,
            page_id,
            chunk_id,
            provider,
            model_version,
            payload["duration_ms"],
            payload["token_input"],
            payload["token_output"],
            cache_hit,
            success,
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    append_jsonl(settings.costly_calls_jsonl, payload)


def fallback_event_id(document_id: str, page_id: str | None, trigger_reason: str, page_hash: str | None) -> str:
    raw = f"{document_id}|{page_id or ''}|{trigger_reason}|{page_hash or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def record_fallback_event(
    settings: Settings,
    conn: Connection,
    *,
    document_id: str,
    page_id: str | None,
    source_tier: str,
    trigger_reason: str,
    region: str | None,
    page_hash: str | None,
    model_version: str | None,
) -> None:
    event_id = fallback_event_id(document_id, page_id, trigger_reason, page_hash)
    exists = conn.execute("SELECT 1 FROM fallback_events WHERE event_id = %s", (event_id,)).fetchone()
    if exists:
        return

    conn.execute(
        """
        INSERT INTO fallback_events (
            event_id, document_id, page_id, source_tier,
            trigger_reason, region, page_hash, model_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (event_id, document_id, page_id, source_tier, trigger_reason, region, page_hash, model_version),
    )
    append_jsonl(
        settings.fallback_events_jsonl,
        {
            "event_id": event_id,
            "document_id": document_id,
            "page_id": page_id,
            "source_tier": source_tier,
            "trigger_reason": trigger_reason,
            "region": region,
            "page_hash": page_hash,
            "model_version": model_version,
        },
    )
