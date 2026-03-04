from __future__ import annotations

import json
import sqlite3
import wave
from pathlib import Path

from agentic_parse.chunk_embed import chunk_and_embed
from agentic_parse.config import Settings
from agentic_parse.db import connect, init_schema
from agentic_parse.entities import extract_entities, extract_for_query
from agentic_parse.extract_text import extract_text
from agentic_parse.ingest import ingest
from agentic_parse.paystub import extract_paystubs
from agentic_parse.summarize import summarize


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c6360000000020001e221bc330000000049454e44ae426082"
)


def _make_wav(path: Path, seconds: int = 1) -> None:
    with wave.open(path.as_posix(), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        frames = b"\x00\x00" * 16000 * seconds
        handle.writeframes(frames)


def _setup_raw(raw: Path) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "doc.txt").write_text("Alice Johnson met Bob Stone on 2025-01-02.", encoding="utf-8")
    (raw / "paystub_sample.txt").write_text(
        "Pay Period: 2025-01-01 to 2025-01-15\nPay Date: 2025-01-20\nGross Pay: $1200.00\nNet Pay: $1400.00\n",
        encoding="utf-8",
    )
    (raw / "receipt_sheet.txt").write_text(
        "Receipt #44\nLine item A 12.50\nLine item B 7.50\nTotal: $20.00\n",
        encoding="utf-8",
    )
    (raw / "messages_chat.json").write_text(
        json.dumps(
            [
                {"timestamp": "2025-01-01 10:00", "sender": "A", "message": "hello"},
                {"timestamp": "2025-01-01 10:05", "sender": "B", "message": "hi"},
            ]
        ),
        encoding="utf-8",
    )
    (raw / "screenshot_1.png").write_bytes(PNG_1X1)
    _make_wav(raw / "sample.wav", seconds=1)
    (raw / "sample.mp4").write_bytes(b"not-a-real-video-but-pipeline-handles-placeholder")
    (raw / "sample.pdf").write_bytes(b"%PDF-1.3\n% minimal placeholder\n")


def _new_settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "workspace"
    raw = tmp_path / "raw"
    _setup_raw(raw)
    settings = Settings(workspace=workspace, raw_root=raw)
    settings.ensure_dirs()
    return settings


def _open_db(settings: Settings) -> sqlite3.Connection:
    conn = connect(settings.db_path)
    init_schema(conn)
    return conn


def test_ingest_dedup_and_catalog_fields(tmp_path: Path) -> None:
    settings = _new_settings(tmp_path)
    conn = _open_db(settings)

    first = ingest(settings, conn)
    second = ingest(settings, conn)

    assert first >= 6
    assert second == 0

    rows = conn.execute("SELECT document_id, sha256, media_type, summary_status FROM documents").fetchall()
    assert rows
    assert all(r["document_id"].startswith("doc_") for r in rows)
    assert all(r["sha256"] for r in rows)
    assert all(r["media_type"] for r in rows)
    assert all(r["summary_status"] == "pending_transcription" for r in rows)


def test_incremental_pipeline_and_metrics(tmp_path: Path) -> None:
    settings = _new_settings(tmp_path)
    conn = _open_db(settings)

    ingest(settings, conn)
    first_pages = extract_text(settings, conn, workers=2)
    first_chunks = chunk_and_embed(settings, conn)
    first_summaries = summarize(settings, conn)
    first_entities, _ = extract_entities(settings, conn)
    first_paystubs, _ = extract_paystubs(settings, conn)

    assert first_pages >= 1
    assert first_chunks >= 1
    assert first_summaries >= 1
    assert first_entities >= 0
    assert first_paystubs >= 1

    second_pages = extract_text(settings, conn, workers=2)
    second_chunks = chunk_and_embed(settings, conn)
    second_summaries = summarize(settings, conn)

    assert second_pages == 0
    assert second_chunks == 0
    assert second_summaries == 0

    metric_count = conn.execute("SELECT COUNT(*) FROM stage_metrics").fetchone()[0]
    assert metric_count >= 5


def test_retrieval_caps_enforced(tmp_path: Path) -> None:
    settings = _new_settings(tmp_path)
    conn = _open_db(settings)

    ingest(settings, conn)
    extract_text(settings, conn, workers=2)
    chunk_and_embed(settings, conn)

    try:
        extract_for_query(
            settings,
            conn,
            query="Who met whom?",
            top_k=10,
            max_chunks=5,
            max_tokens=4000,
        )
        raise AssertionError("expected ValueError for chunk cap")
    except ValueError:
        pass


def test_payment_record_extraction_and_validation(tmp_path: Path) -> None:
    settings = _new_settings(tmp_path)
    conn = _open_db(settings)

    ingest(settings, conn)
    extract_text(settings, conn, workers=2)
    processed, needs_review = extract_paystubs(settings, conn)

    assert processed >= 2
    assert needs_review >= 1

    row = conn.execute("SELECT validation_status, document_type FROM paystubs ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["validation_status"] in {"valid", "needs_review"}
    assert row["document_type"] in {"pay_stub", "receipt", "invoice", "payment_sheet", "payment_record"}
