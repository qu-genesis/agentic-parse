from __future__ import annotations

import hashlib
import json
import sqlite3

from .config import Settings
from .utils import append_jsonl


def record_stage_metric(
    settings: Settings,
    conn: sqlite3.Connection,
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
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


def fallback_event_id(document_id: str, page_id: str | None, trigger_reason: str, page_hash: str | None) -> str:
    raw = f"{document_id}|{page_id or ''}|{trigger_reason}|{page_hash or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def record_fallback_event(
    settings: Settings,
    conn: sqlite3.Connection,
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
    exists = conn.execute("SELECT 1 FROM fallback_events WHERE event_id = ?", (event_id,)).fetchone()
    if exists:
        return

    conn.execute(
        """
        INSERT INTO fallback_events (
            event_id, document_id, page_id, source_tier,
            trigger_reason, region, page_hash, model_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
