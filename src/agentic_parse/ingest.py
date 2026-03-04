from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .db import Connection
from .telemetry import record_stage_metric
from .utils import (
    append_jsonl,
    file_sha256,
    media_type_for,
    probe_media_duration_seconds,
    short_doc_id,
)


PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
CHAT_EXPORT_SUFFIXES = {".json", ".csv"}


@dataclass
class PdfInfo:
    page_count: int | None
    has_text_layer: bool | None


def _classify_family(path: Path) -> str:
    mime = media_type_for(path)
    if path.suffix.lower() in CHAT_EXPORT_SUFFIXES and "chat" in path.name.lower():
        return "chat_export"
    if mime.startswith("text/"):
        return "text"
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        if "screenshot" in path.name.lower():
            return "screenshot"
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return "other"


def _scan_pdf(path: Path) -> PdfInfo:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return PdfInfo(page_count=None, has_text_layer=None)

    try:
        reader = PdfReader(path.as_posix())
        pages = len(reader.pages)
        sampled_text = ""
        for idx in range(min(5, pages)):
            sampled_text += reader.pages[idx].extract_text() or ""
        has_text = bool(sampled_text.strip())
        return PdfInfo(page_count=pages, has_text_layer=has_text)
    except Exception:
        return PdfInfo(page_count=None, has_text_layer=None)


def ingest(settings: Settings, conn: Connection) -> int:
    inserted = 0
    skipped = 0
    files = [p for p in settings.raw_root.rglob("*") if p.is_file()]
    for path in files:
        sha256 = file_sha256(path)
        document_id = short_doc_id(sha256)

        existing = conn.execute(
            "SELECT 1 FROM documents WHERE document_id = %s", (document_id,)
        ).fetchone()
        if existing:
            skipped += 1
            continue

        family = _classify_family(path)
        page_count = None
        has_text_layer = None
        audio_duration = None
        video_duration = None
        if family == "pdf":
            info = _scan_pdf(path)
            page_count = info.page_count
            has_text_layer = None if info.has_text_layer is None else int(info.has_text_layer)
        elif family == "audio":
            audio_duration = probe_media_duration_seconds(path)
        elif family == "video":
            video_duration = probe_media_duration_seconds(path)

        conn.execute(
            """
            INSERT INTO documents (
                document_id, sha256, path, media_type, doc_family, size_bytes,
                page_count, has_text_layer, audio_duration_seconds,
                video_duration_seconds, summary_status, status_ingest
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_transcription', 'done')
            """,
            (
                document_id,
                sha256,
                path.as_posix(),
                media_type_for(path),
                family,
                path.stat().st_size,
                page_count,
                has_text_layer,
                audio_duration,
                video_duration,
            ),
        )

        append_jsonl(
            settings.catalogue_jsonl,
            {
                "document_id": document_id,
                "sha256": sha256,
                "path": path.as_posix(),
                "media_type": media_type_for(path),
                "doc_family": family,
                "size_bytes": path.stat().st_size,
                "page_count": page_count,
                "has_text_layer": None if has_text_layer is None else bool(has_text_layer),
                "audio_duration_seconds": audio_duration,
                "video_duration_seconds": video_duration,
                "summary_status": "pending_transcription",
            },
        )
        inserted += 1
    record_stage_metric(settings, conn, "ingest", processed=inserted, skipped=skipped, failed=0)
    conn.commit()
    return inserted
