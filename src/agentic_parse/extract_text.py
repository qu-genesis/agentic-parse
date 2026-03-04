from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import csv
import hashlib
import json
from pathlib import Path

from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_fallback_event, record_stage_metric
from .utils import atomic_write_text, page_hash, probe_media_duration_seconds


@dataclass
class PageResult:
    page_id: str
    document_id: str
    page_number: int
    source_tier: str
    text_path: str
    confidence: float
    hash_value: str
    source_pointer: str | None = None
    timestamp_start_ms: int | None = None
    timestamp_end_ms: int | None = None
    fallback_trigger_reason: str | None = None
    fallback_region: str | None = None


def _page_id(document_id: str, page_number: int) -> str:
    return f"{document_id}_p{page_number:04d}"


def _write_page_text(settings: Settings, document_id: str, page_number: int, text: str) -> str:
    out = settings.transcripts_dir / document_id / f"page_{page_number:04d}.txt"
    atomic_write_text(out, text)
    return out.as_posix()


def _run_tier0_pdf_text(path: Path) -> list[str] | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None

    try:
        reader = PdfReader(path.as_posix())
        return [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception:
        return None


def _ocr_confidence_from_data(data: dict) -> float:
    values = []
    for raw in data.get("conf", []):
        try:
            v = float(raw)
        except Exception:
            continue
        if v >= 0:
            values.append(v)
    if not values:
        return 0.0
    return max(0.0, min(1.0, (sum(values) / len(values)) / 100.0))


def _run_tier1_ocr_image(path: Path) -> tuple[str, float] | None:
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        img = Image.open(path.as_posix())
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        text = " ".join([t for t in data.get("text", []) if str(t).strip()]).strip()
        conf = _ocr_confidence_from_data(data)
        return text, conf
    except Exception:
        return None


def _run_tier1_ocr_pdf_page(path: Path, page_number: int) -> tuple[str, float] | None:
    try:
        import pytesseract  # type: ignore
        import pypdfium2 as pdfium  # type: ignore
    except Exception:
        return None

    try:
        doc = pdfium.PdfDocument(path.as_posix())
        page = doc[page_number - 1]
        pil = page.render(scale=2.0).to_pil()
        data = pytesseract.image_to_data(pil, output_type=pytesseract.Output.DICT)
        text = " ".join([t for t in data.get("text", []) if str(t).strip()]).strip()
        conf = _ocr_confidence_from_data(data)
        return text, conf
    except Exception:
        return None


def _fallback_cache_key(page_hash_value: str, model_version: str) -> str:
    return hashlib.sha256(f"{model_version}|{page_hash_value}".encode("utf-8")).hexdigest()


def _run_tier2_llm_fallback(
    settings: Settings,
    *,
    content: str,
    page_hash_value: str,
    model_version: str = "gpt-4o-mini",
) -> tuple[str, float]:
    llm = get_llm_client()
    system_prompt = (
        "You recover text from noisy OCR output. Return only cleaned text. "
        "Do not add facts not present in input."
    )
    user_prompt = f"OCR text:\n{content}\n\nReturn cleaned plain text only."
    recovered = llm.text(
        task="tier2_ocr_cleanup",
        cache_dir=settings.fallback_cache_dir,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_output_tokens=1200,
        cache_key_override=_fallback_cache_key(page_hash_value, model_version),
    )
    if recovered is None:
        recovered = content.strip()
    recovered = recovered.strip()
    confidence = 0.75 if recovered else 0.0
    return recovered, confidence


def _extract_pdf_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    pages = _run_tier0_pdf_text(path)
    if pages is None:
        try:
            import pypdfium2 as pdfium  # type: ignore

            doc = pdfium.PdfDocument(path.as_posix())
            pages = ["" for _ in range(len(doc))]
        except Exception:
            return []

    results: list[PageResult] = []
    for idx, text in enumerate(pages, start=1):
        source_tier = "tier0_text_layer"
        confidence = 0.99 if text else 0.0
        fallback_trigger_reason = None
        fallback_region = None

        if not text:
            ocr = _run_tier1_ocr_pdf_page(path, idx)
            if ocr:
                text, confidence = ocr
                source_tier = "tier1_ocr_pdf"

            if confidence < 0.35:
                raw_hash = page_hash(text)
                text, confidence = _run_tier2_llm_fallback(
                    settings,
                    content=text,
                    page_hash_value=raw_hash,
                    model_version=get_llm_client().model,
                )
                source_tier = "tier2_llm_fallback"
                fallback_trigger_reason = "low_ocr_confidence"
                fallback_region = "full_page"

        text_hash = page_hash(text)
        out = _write_page_text(settings, document_id, idx, text)
        pid = _page_id(document_id, idx)
        results.append(
            PageResult(
                page_id=pid,
                document_id=document_id,
                page_number=idx,
                source_tier=source_tier,
                text_path=out,
                confidence=confidence,
                hash_value=text_hash,
                source_pointer=f"page:{idx}",
                fallback_trigger_reason=fallback_trigger_reason,
                fallback_region=fallback_region,
            )
        )
    return results


def _extract_image_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    tier1 = _run_tier1_ocr_image(path)
    text = ""
    source_tier = "tier1_ocr_image"
    confidence = 0.0
    fallback_trigger_reason = None
    fallback_region = None

    if tier1:
        text, confidence = tier1

    if confidence < 0.3:
        raw_hash = page_hash(text)
        text, confidence = _run_tier2_llm_fallback(
            settings,
            content=text,
            page_hash_value=raw_hash,
            model_version=get_llm_client().model,
        )
        source_tier = "tier2_llm_fallback"
        fallback_trigger_reason = "low_ocr_confidence"
        fallback_region = "full_page"

    page_num = 1
    out = _write_page_text(settings, document_id, page_num, text)
    return [
        PageResult(
            page_id=_page_id(document_id, page_num),
            document_id=document_id,
            page_number=page_num,
            source_tier=source_tier,
            text_path=out,
            confidence=confidence,
            hash_value=page_hash(text),
            source_pointer="page:1",
            fallback_trigger_reason=fallback_trigger_reason,
            fallback_region=fallback_region,
        )
    ]


def _extract_text_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    page_num = 1
    out = _write_page_text(settings, document_id, page_num, text)
    return [
        PageResult(
            page_id=_page_id(document_id, page_num),
            document_id=document_id,
            page_number=page_num,
            source_tier="tier0_plain_text",
            text_path=out,
            confidence=0.99 if text else 0.0,
            hash_value=page_hash(text),
            source_pointer="page:1",
        )
    ]


def _extract_chat_export_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    items: list[str] = []
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(payload, list):
                for row in payload:
                    if isinstance(row, dict):
                        ts = row.get("timestamp") or row.get("date") or ""
                        sender = row.get("sender") or row.get("from") or "unknown"
                        msg = row.get("message") or row.get("text") or ""
                        items.append(f"[{ts}] {sender}: {msg}".strip())
        elif path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    ts = row.get("timestamp") or row.get("date") or ""
                    sender = row.get("sender") or row.get("from") or "unknown"
                    msg = row.get("message") or row.get("text") or ""
                    items.append(f"[{ts}] {sender}: {msg}".strip())
    except Exception:
        items = []

    text = "\n".join([x for x in items if x.strip()]).strip()
    if not text:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()

    page_num = 1
    out = _write_page_text(settings, document_id, page_num, text)
    return [
        PageResult(
            page_id=_page_id(document_id, page_num),
            document_id=document_id,
            page_number=page_num,
            source_tier="tier0_chat_export_parser",
            text_path=out,
            confidence=0.99 if text else 0.0,
            hash_value=page_hash(text),
            source_pointer="thread:parsed_export",
        )
    ]


def _fallback_asr_segments(duration_seconds: float | None) -> list[dict]:
    duration = int(max(1.0, duration_seconds or 30.0))
    segments = []
    step = 30
    start = 0
    while start < duration:
        end = min(duration, start + step)
        segments.append(
            {
                "start_ms": int(start * 1000),
                "end_ms": int(end * 1000),
                "text": "[UNTRANSCRIBED - ASR unavailable]",
            }
        )
        start = end
    return segments


def _extract_audio_or_video_document(
    settings: Settings,
    document_id: str,
    path: Path,
    family: str,
) -> list[PageResult]:
    llm = get_llm_client()
    segments = llm.transcribe_audio(path)
    duration_seconds = probe_media_duration_seconds(path)
    if not segments:
        segments = _fallback_asr_segments(duration_seconds)

    if family == "video":
        # Sparse keyframe pointers for provenance. Actual keyframe extraction can be plugged in later.
        keyframe_step_ms = 15000
    else:
        keyframe_step_ms = 0

    results: list[PageResult] = []
    for idx, seg in enumerate(segments, start=1):
        text = str(seg.get("text", "")).strip()
        start_ms = int(seg.get("start_ms", 0))
        end_ms = int(seg.get("end_ms", start_ms))
        if keyframe_step_ms > 0:
            source_pointer = f"timestamp:{start_ms}-{end_ms};keyframe_every_ms:{keyframe_step_ms}"
        else:
            source_pointer = f"timestamp:{start_ms}-{end_ms}"
        out = _write_page_text(settings, document_id, idx, text)
        results.append(
            PageResult(
                page_id=_page_id(document_id, idx),
                document_id=document_id,
                page_number=idx,
                source_tier="tier0_asr" if text and "UNTRANSCRIBED" not in text else "tier0_asr_placeholder",
                text_path=out,
                confidence=0.8 if text and "UNTRANSCRIBED" not in text else 0.1,
                hash_value=page_hash(text),
                source_pointer=source_pointer,
                timestamp_start_ms=start_ms,
                timestamp_end_ms=end_ms,
            )
        )
    return results


def _process_document(settings: Settings, row) -> list[PageResult]:
    document_id = row["document_id"]
    path = Path(row["path"])
    family = row["doc_family"]

    if family == "pdf":
        return _extract_pdf_document(settings, document_id, path)
    if family == "image" or family == "screenshot":
        return _extract_image_document(settings, document_id, path)
    if family == "text":
        return _extract_text_document(settings, document_id, path)
    if family == "audio" or family == "video":
        return _extract_audio_or_video_document(settings, document_id, path, family)
    if family == "chat_export":
        return _extract_chat_export_document(settings, document_id, path)
    return []


def extract_text(settings: Settings, conn: Connection, workers: int = 4) -> int:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    rows = conn.execute(
        "SELECT * FROM documents WHERE status_ocr != 'done' OR status_asr != 'done' ORDER BY created_at ASC"
    ).fetchall()
    if not rows:
        record_stage_metric(settings, conn, "extract_text", processed=0, skipped=0, failed=0)
        conn.commit()
        return 0

    inserted_pages = 0
    skipped = 0
    failed = 0
    tier_counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_process_document, settings, row) for row in rows]
        for future in as_completed(futures):
            try:
                page_results = future.result()
            except Exception:
                failed += 1
                continue
            touched_docs: set[str] = set()
            for result in page_results:
                existing = conn.execute(
                    "SELECT 1 FROM pages WHERE page_id = %s", (result.page_id,)
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO pages (
                        page_id, document_id, page_number, source_tier, source_pointer,
                        ocr_status, ocr_confidence, text_path, page_hash,
                        timestamp_start_ms, timestamp_end_ms,
                        fallback_trigger_reason, fallback_region
                    ) VALUES (%s, %s, %s, %s, %s, 'done', %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        result.page_id,
                        result.document_id,
                        result.page_number,
                        result.source_tier,
                        result.source_pointer,
                        result.confidence,
                        result.text_path,
                        result.hash_value,
                        result.timestamp_start_ms,
                        result.timestamp_end_ms,
                        result.fallback_trigger_reason,
                        result.fallback_region,
                    ),
                )
                if result.fallback_trigger_reason:
                    record_fallback_event(
                        settings,
                        conn,
                        document_id=result.document_id,
                        page_id=result.page_id,
                        source_tier=result.source_tier,
                        trigger_reason=result.fallback_trigger_reason,
                        region=result.fallback_region,
                        page_hash=result.hash_value,
                        model_version=llm.model,
                    )
                touched_docs.add(result.document_id)
                inserted_pages += 1
                tier_counts[result.source_tier] = tier_counts.get(result.source_tier, 0) + 1

            for doc_id in touched_docs:
                conn.execute(
                    """
                    UPDATE documents
                    SET status_ocr = 'done',
                        status_asr = CASE WHEN doc_family IN ('audio','video') THEN 'done' ELSE status_asr END,
                        summary_status = 'ready_for_summary',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE document_id = %s
                    """,
                    (doc_id,),
                )

    after_in, after_out = llm.usage_snapshot()
    record_stage_metric(
        settings,
        conn,
        "extract_text",
        processed=inserted_pages,
        skipped=skipped,
        failed=failed,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
        metadata={"tier_counts": tier_counts},
    )
    conn.commit()
    return inserted_pages
