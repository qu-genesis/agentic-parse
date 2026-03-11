from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from tqdm import tqdm

# pypdfium2 and pytesseract share native global state and segfault under
# concurrent threads. Serialise all calls through a single process-wide lock.
_NATIVE_OCR_LOCK = threading.Lock()

from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_costly_call, record_fallback_event, record_stage_metric
from .utils import atomic_write_text, page_hash, probe_media_duration_seconds


# ── Step 1: Text quality scoring ─────────────────────────────────────────────

_TEXT_QUALITY_THRESHOLD = 0.45  # below this → escalate to OCR

_COMMON_WORD_RE = re.compile(r"\b[a-zA-Z]{3,}\b")
_NUMERIC_TOKEN_RE = re.compile(r"\b[\d,.$%]+\b")


def _text_quality_score(text: str) -> float:
    """Return a 0.0–1.0 quality score. Lower = worse / more likely needs OCR."""
    if not text or not text.strip():
        return 0.0

    stripped = text.strip()
    total = len(stripped)
    if total < 30:
        return 0.1

    alpha = sum(c.isalpha() for c in stripped)
    printable = sum(c.isprintable() for c in stripped)
    unique_ratio = len(set(stripped)) / min(total, 200)

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    short_line_ratio = (
        sum(1 for ln in lines if len(ln) <= 2) / len(lines) if lines else 1.0
    )

    ws = sum(c.isspace() for c in text)
    ws_ratio = ws / max(1, len(text))

    word_count = len(_COMMON_WORD_RE.findall(stripped))
    word_density = min(1.0, word_count / max(1, total / 5))

    score = (
        0.25 * min(1.0, (alpha / total) / 0.5)
        + 0.20 * (printable / total)
        + 0.15 * min(1.0, unique_ratio / 0.3)
        + 0.15 * (1.0 - short_line_ratio)
        + 0.10 * (1.0 - ws_ratio)
        + 0.15 * word_density
    )
    return round(max(0.0, min(1.0, score)), 3)


def _is_bad_text_layer(text: str) -> tuple[bool, str]:
    """Return (is_bad, reason). Reason is logged as the fallback trigger."""
    if not text or len(text.strip()) < 30:
        return True, "empty_or_near_empty"

    stripped = text.strip()
    total = len(stripped)
    alpha = sum(c.isalpha() for c in stripped)
    if alpha / total < 0.20:
        return True, "low_alpha_ratio"

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if lines and sum(1 for ln in lines if len(ln) <= 2) / len(lines) > 0.5:
        return True, "vertical_or_fragmented_text"

    if _text_quality_score(text) < _TEXT_QUALITY_THRESHOLD:
        return True, "bad_text_layer"

    return False, ""


# ── Step 2: Multi-backend Tier-0 text extraction ──────────────────────────────

def _tier0a_pymupdf(path: Path, page_number: int) -> str | None:
    """PyMuPDF (fitz) — best for complex layouts and mixed content."""
    try:
        import fitz  # type: ignore
        doc = fitz.open(path.as_posix())
        page = doc[page_number - 1]
        text = page.get_text("text")
        doc.close()
        return text.strip() or None
    except Exception:
        return None


def _tier0b_pdfplumber(path: Path, page_number: int) -> str | None:
    """pdfplumber — strong on tables and dense column layouts."""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path.as_posix()) as pdf:
            if page_number - 1 >= len(pdf.pages):
                return None
            text = pdf.pages[page_number - 1].extract_text() or ""
            return text.strip() or None
    except Exception:
        return None


def _tier0c_pypdf(path: Path, page_number: int) -> str | None:
    """pypdf — fast, good for simple text-layer PDFs."""
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(path.as_posix())
        if page_number - 1 >= len(reader.pages):
            return None
        text = reader.pages[page_number - 1].extract_text() or ""
        return text.strip() or None
    except Exception:
        return None


def _best_tier0_text(path: Path, page_number: int) -> tuple[str, float, str]:
    """Try all Tier-0 backends; pick the one with the highest quality score."""
    candidates = [
        (_tier0a_pymupdf(path, page_number), "tier0_pymupdf"),
        (_tier0b_pdfplumber(path, page_number), "tier0_pdfplumber"),
        (_tier0c_pypdf(path, page_number), "tier0_pypdf"),
    ]
    best_text, best_score, best_name = "", 0.0, "tier0_pypdf"
    for text, name in candidates:
        if text is None:
            continue
        score = _text_quality_score(text)
        if score > best_score:
            best_text, best_score, best_name = text, score, name
    return best_text, best_score, best_name


def _pdf_page_count(path: Path) -> int | None:
    """Get page count using the best available backend."""
    for attempt in [
        lambda: __import__("fitz").open(path.as_posix()).__len__(),
        lambda: len(__import__("pypdf").PdfReader(path.as_posix()).pages),
    ]:
        try:
            return attempt()
        except Exception:
            continue
    try:
        import pypdfium2 as pdfium  # type: ignore
        with _NATIVE_OCR_LOCK:
            return len(pdfium.PdfDocument(path.as_posix()))
    except Exception:
        return None


# ── Step 3: Tier-1 OCR with orientation search + preprocessing ───────────────

def _render_page_pil(path: Path, page_number: int, scale: float = 2.0, rotation: int = 0):
    """Render a PDF page to a PIL image (pypdfium2)."""
    try:
        import pypdfium2 as pdfium  # type: ignore
        with _NATIVE_OCR_LOCK:
            doc = pdfium.PdfDocument(path.as_posix())
            page = doc[page_number - 1]
            return page.render(scale=scale, rotation=rotation).to_pil()
    except Exception:
        return None


def _preprocess_for_ocr(pil_image):
    """Grayscale + sharpen to improve tesseract accuracy."""
    try:
        from PIL import ImageFilter  # type: ignore
        return pil_image.convert("L").filter(ImageFilter.SHARPEN)
    except Exception:
        return pil_image


def _ocr_pil(pil_image) -> tuple[str, float]:
    """Run tesseract on a PIL image; return (text, normalised_confidence)."""
    try:
        import pytesseract  # type: ignore
        with _NATIVE_OCR_LOCK:
            data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
        text = " ".join(t for t in data.get("text", []) if str(t).strip()).strip()
        conf_vals = [float(v) for v in data.get("conf", []) if float(v) >= 0]
        conf = (sum(conf_vals) / len(conf_vals) / 100.0) if conf_vals else 0.0
        return text, max(0.0, min(1.0, conf))
    except Exception:
        return "", 0.0


def _run_tier1_pdf_page(path: Path, page_number: int) -> tuple[str, float] | None:
    """OCR with 4-rotation search; return (best_text, best_conf) or None."""
    best_text, best_conf = "", 0.0
    any_rendered = False
    for rotation in [0, 90, 180, 270]:
        pil = _render_page_pil(path, page_number, scale=2.0, rotation=rotation)
        if pil is None:
            continue
        any_rendered = True
        processed = _preprocess_for_ocr(pil)
        text, conf = _ocr_pil(processed)
        if conf > best_conf:
            best_text, best_conf = text, conf
    return (best_text, best_conf) if any_rendered else None


def _run_tier1_ocr_image(path: Path) -> tuple[str, float] | None:
    """OCR for standalone image files."""
    try:
        from PIL import Image  # type: ignore
        pil = Image.open(path.as_posix())
    except Exception:
        return None
    processed = _preprocess_for_ocr(pil)
    text, conf = _ocr_pil(processed)
    return (text, conf) if (text or conf > 0) else None


# ── Step 4: Tier-2A (LLM cleanup) and Tier-2B (vision OCR) ──────────────────

def _fallback_cache_key(tier: str, page_hash_value: str, model_version: str) -> str:
    material = f"{tier}|{model_version}|{page_hash_value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _image_hash(pil_image) -> str:
    """SHA-256 of the raw image bytes — used as the Tier-2B cache key so
    each unique page image gets its own vision-OCR result."""
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return hashlib.sha256(buf.getvalue()).hexdigest()


def _run_tier2a_llm_cleanup(
    settings: Settings,
    *,
    content: str,
    page_hash_value: str,
    model_version: str,
) -> tuple[str, float]:
    """Tier-2A: LLM text-in/text-out cleanup for noisy OCR output."""
    llm = get_llm_client()
    recovered = llm.text(
        task="tier2a_ocr_cleanup",
        cache_dir=settings.fallback_cache_dir,
        system_prompt=(
            "You are a document text restorer for noisy OCR from invoices, receipts, forms, "
            "letters, reports, tables, and handwritten inserts. "
            "Your job is to fix OCR artifacts without changing meaning.\n\n"
            "Rules:\n"
            "- Output plain text only (no markdown, no explanations).\n"
            "- Do not add or infer missing facts. If unsure, keep the original token.\n"
            "- Preserve numbers, decimals, commas, currency symbols, dates, and ID-like strings "
            "exactly unless an OCR error is obvious (e.g., 'O' vs '0' in a numeric-only field).\n"
            "- Fix common OCR issues: broken words across line breaks, hyphenation artifacts, "
            "duplicated headers/footers when clearly repeated, random ligatures, garbled spacing.\n"
            "- Preserve line breaks where they indicate rows/columns/fields; otherwise normalize spacing.\n"
            "- If a character/word is unreadable, keep it as-is; do not replace with guesses."
        ),
        user_prompt=(
            "OCR output (may include broken words, random line breaks, misread characters like "
            "O/0, l/1, S/5, duplicated headers/footers, and spacing issues).\n"
            "Clean it while preserving meaning and all numeric values.\n\n"
            f"OCR TEXT:\n{content}\n\n"
            "Return cleaned plain text only."
        ),
        max_output_tokens=1200,
        cache_key_override=_fallback_cache_key("2a", page_hash_value, model_version),
    )
    recovered = (recovered or content or "").strip()
    return recovered, (0.75 if recovered else 0.0)


def _run_tier2b_vision_ocr(
    settings: Settings,
    *,
    pil_image,
    model_version: str,
    page_type: str = "unknown",
) -> tuple[str, float]:
    """Tier-2B: Render page → vision LLM for faithful transcription.
    Cache key is derived from the image content so each unique page gets
    its own result (avoids collisions when text is empty across many pages).
    """
    cache_key = _fallback_cache_key("2b", _image_hash(pil_image), model_version)
    cache_path = settings.fallback_cache_dir / f"{cache_key}.json"

    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        text = payload.get("text", "")
        return text, (0.82 if text else 0.0)

    llm = get_llm_client()
    if not llm.enabled:
        return "", 0.0

    type_hints = {
        "table": "Preserve table rows and column alignment precisely. Transcribe column by column, top to bottom, separating columns with a blank line.",
        "form": "Preserve all field labels, values, and checkbox states. Use [ ] for unchecked and [x] for checked boxes.",
        "handwritten": "Transcribe handwritten text faithfully. Mark unreadable words as [illegible] or [illegible: N words] if you can estimate length.",
        "invoice": "Preserve line items, amounts, dates, and party names exactly. Preserve line breaks where they indicate rows.",
        "comment_box": "Preserve author name, timestamp, and full comment text in reading order.",
    }
    hint = type_hints.get(page_type, "Transcribe in the reading order that makes the text logical. For multi-column pages, transcribe column by column, top to bottom, separated by a blank line.")

    _vision_system_prompt = (
        "You are a precise page transcription tool for PDFs that may contain scanned receipts, "
        "invoices, tables, forms, handwritten notes, and mixed layouts "
        "(multi-column, landscape, rotated, skewed).\n\n"
        "Rules:\n"
        "- Output only the transcribed text in reading order, with line breaks that preserve structure.\n"
        "- Do not summarize. Do not explain. Do not infer missing text.\n"
        "- If text is unreadable, write [illegible] (or [illegible: N words] if you can estimate length).\n"
        "- Preserve numbers, currency, dates, totals, and line items exactly as seen.\n"
        "- Preserve line breaks where they indicate rows/columns/fields; otherwise normalize spacing.\n"
        "- For rotated/landscape pages: transcribe in the orientation that makes the text readable.\n"
        "- Signatures or initials that are not legible text: write [signature] or [initials]."
    )

    try:
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        response = llm._client.responses.create(
            model=llm.model,
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": _vision_system_prompt}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{img_b64}",
                        },
                        {
                            "type": "input_text",
                            "text": (
                                f"Transcribe all visible text from this single page.\n{hint}\n\n"
                                "Conventions:\n"
                                "- Unreadable text → [illegible]\n"
                                "- Unchecked boxes → [ ]  |  Checked boxes → [x]\n"
                                "- Unreadable signatures/initials → [signature] or [initials]\n\n"
                                "Return only the transcription text, nothing else."
                            ),
                        },
                    ],
                },
            ],
            max_output_tokens=1500,
        )
        text = (getattr(response, "output_text", "") or "").strip()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"task": "tier2b_vision_ocr", "text": text}),
            encoding="utf-8",
        )
        return text, (0.82 if text else 0.0)
    except Exception:
        return "", 0.0


# ── Step 5: Page-type classifier ─────────────────────────────────────────────

_INVOICE_RE = re.compile(
    r"\b(invoice|receipt|bill to|ship to|subtotal|total due|tax|amount due|payment|line item)\b",
    re.IGNORECASE,
)
_CHECKBOX_RE = re.compile(r"[☐☑✓✗□■]|\[\s*[xX\s]\s*\]")
_FORM_FIELD_RE = re.compile(
    r"\b(name|date|signature|address|phone|email|ssn|dob)\s*[:\-_]",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(
    r"\b(comment|response|author|reply|note by|reviewed by)\b", re.IGNORECASE
)


def _classify_page_type(text: str, ocr_confidence: float) -> str:
    """Classify page archetype using deterministic heuristics."""
    if not text or not text.strip():
        return "handwritten" if ocr_confidence < 0.2 else "blank"

    if len(_INVOICE_RE.findall(text)) >= 2:
        return "invoice"

    checkboxes = len(_CHECKBOX_RE.findall(text))
    form_fields = len(_FORM_FIELD_RE.findall(text))
    if checkboxes >= 2 or form_fields >= 3:
        return "form"

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        numeric_lines = sum(
            1 for ln in lines if len(_NUMERIC_TOKEN_RE.findall(ln)) >= 3
        )
        ws_aligned = sum(1 for ln in lines if len(re.findall(r"\s{2,}", ln)) >= 2)
        if numeric_lines / len(lines) > 0.30 or ws_aligned / len(lines) > 0.40:
            return "table"

    if ocr_confidence < 0.35 and ocr_confidence > 0:
        tokens = text.split()
        if tokens and sum(1 for t in tokens if len(t) <= 3) / len(tokens) > 0.5:
            return "handwritten"

    if _COMMENT_RE.search(text):
        return "comment_box"

    return "text"


# ── PageResult dataclass ──────────────────────────────────────────────────────

@dataclass
class PageResult:
    page_id: str
    document_id: str
    page_number: int
    source_tier: str
    text_path: str
    confidence: float
    hash_value: str
    page_type: str = "text"
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


# ── PDF document extraction (Steps 1-5 integrated) ───────────────────────────

def _extract_pdf_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    llm = get_llm_client()
    page_count = _pdf_page_count(path)
    if page_count is None:
        return []

    results: list[PageResult] = []
    for page_number in range(1, page_count + 1):
        # Step 2: Multi-backend Tier-0 — pick best text
        text, t0_score, t0_backend = _best_tier0_text(path, page_number)

        source_tier = t0_backend
        confidence = t0_score
        fallback_trigger_reason: str | None = None
        fallback_region: str | None = None
        pil_cache: object = None  # lazy-render for Tier-1/2B

        # Step 1: Is Tier-0 text actually good?
        bad, bad_reason = _is_bad_text_layer(text)
        if bad:
            fallback_trigger_reason = bad_reason

            # Step 3: Tier-1 OCR with orientation search
            ocr = _run_tier1_pdf_page(path, page_number)
            if ocr:
                ocr_text, ocr_conf = ocr
                if _text_quality_score(ocr_text) > _text_quality_score(text):
                    text, confidence = ocr_text, ocr_conf
                    source_tier = "tier1_ocr_pdf"
            else:
                confidence = 0.0

            # Step 4: Tier-2 escalation when OCR confidence is still low
            if confidence < 0.5:
                raw_hash = page_hash(text)

                # Step 5: classify early (needed to choose 2A vs 2B)
                preliminary_type = _classify_page_type(text, confidence)

                # Tier-2B (vision OCR) for visually complex page types
                if preliminary_type in ("table", "form", "handwritten", "invoice"):
                    pil_cache = _render_page_pil(path, page_number, scale=2.0, rotation=0)
                    if pil_cache is not None:
                        v_text, v_conf = _run_tier2b_vision_ocr(
                            settings,
                            pil_image=pil_cache,
                            model_version=llm.model,
                            page_type=preliminary_type,
                        )
                        if v_conf > confidence:
                            text, confidence = v_text, v_conf
                            source_tier = "tier2b_vision_ocr"
                            fallback_region = "full_page"

                # Tier-2A (LLM text cleanup) for text/unknown types — or as fallback
                if source_tier != "tier2b_vision_ocr":
                    c_text, c_conf = _run_tier2a_llm_cleanup(
                        settings,
                        content=text,
                        page_hash_value=raw_hash,
                        model_version=llm.model,
                    )
                    if c_conf >= confidence:
                        text, confidence = c_text, c_conf
                        source_tier = "tier2a_llm_cleanup"
                        fallback_region = "full_page"

        # Step 5: Final page-type classification
        page_type = _classify_page_type(text, confidence)

        text_hash = page_hash(text)
        out = _write_page_text(settings, document_id, page_number, text)
        results.append(
            PageResult(
                page_id=_page_id(document_id, page_number),
                document_id=document_id,
                page_number=page_number,
                source_tier=source_tier,
                text_path=out,
                confidence=confidence,
                hash_value=text_hash,
                page_type=page_type,
                source_pointer=f"page:{page_number}",
                fallback_trigger_reason=fallback_trigger_reason,
                fallback_region=fallback_region,
            )
        )
    return results


# ── Image / text / chat / audio document extraction (unchanged logic) ─────────

def _extract_image_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    llm = get_llm_client()
    tier1 = _run_tier1_ocr_image(path)
    text, confidence = ("", 0.0)
    source_tier = "tier1_ocr_image"
    fallback_trigger_reason = None
    fallback_region = None

    if tier1:
        text, confidence = tier1

    if confidence < 0.5:
        raw_hash = page_hash(text)
        page_type_prelim = _classify_page_type(text, confidence)

        try:
            from PIL import Image  # type: ignore
            pil = Image.open(path.as_posix())
        except Exception:
            pil = None

        if pil is not None and page_type_prelim in ("table", "form", "handwritten", "invoice"):
            v_text, v_conf = _run_tier2b_vision_ocr(
                settings,
                pil_image=pil,
                model_version=llm.model,
                page_type=page_type_prelim,
            )
            if v_conf > confidence:
                text, confidence = v_text, v_conf
                source_tier = "tier2b_vision_ocr"

        if source_tier != "tier2b_vision_ocr":
            c_text, c_conf = _run_tier2a_llm_cleanup(
                settings,
                content=text,
                page_hash_value=raw_hash,
                model_version=llm.model,
            )
            if c_conf >= confidence:
                text, confidence = c_text, c_conf
                source_tier = "tier2a_llm_cleanup"

        fallback_trigger_reason = "low_ocr_confidence"
        fallback_region = "full_page"

    page_type = _classify_page_type(text, confidence)
    out = _write_page_text(settings, document_id, 1, text)
    return [
        PageResult(
            page_id=_page_id(document_id, 1),
            document_id=document_id,
            page_number=1,
            source_tier=source_tier,
            text_path=out,
            confidence=confidence,
            hash_value=page_hash(text),
            page_type=page_type,
            source_pointer="page:1",
            fallback_trigger_reason=fallback_trigger_reason,
            fallback_region=fallback_region,
        )
    ]


def _extract_text_document(settings: Settings, document_id: str, path: Path) -> list[PageResult]:
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    out = _write_page_text(settings, document_id, 1, text)
    return [
        PageResult(
            page_id=_page_id(document_id, 1),
            document_id=document_id,
            page_number=1,
            source_tier="tier0_plain_text",
            text_path=out,
            confidence=0.99 if text else 0.0,
            hash_value=page_hash(text),
            page_type=_classify_page_type(text, 0.99),
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

    text = "\n".join(x for x in items if x.strip()).strip()
    if not text:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()

    out = _write_page_text(settings, document_id, 1, text)
    return [
        PageResult(
            page_id=_page_id(document_id, 1),
            document_id=document_id,
            page_number=1,
            source_tier="tier0_chat_export_parser",
            text_path=out,
            confidence=0.99 if text else 0.0,
            hash_value=page_hash(text),
            page_type="text",
            source_pointer="thread:parsed_export",
        )
    ]


def _fallback_asr_segments(duration_seconds: float | None) -> list[dict]:
    duration = int(max(1.0, duration_seconds or 30.0))
    segments, step, start = [], 30, 0
    while start < duration:
        end = min(duration, start + step)
        segments.append(
            {"start_ms": int(start * 1000), "end_ms": int(end * 1000), "text": "[UNTRANSCRIBED - ASR unavailable]"}
        )
        start = end
    return segments


def _extract_audio_or_video_document(
    settings: Settings, document_id: str, path: Path, family: str
) -> list[PageResult]:
    llm = get_llm_client()
    segments = llm.transcribe_audio(path)
    duration_seconds = probe_media_duration_seconds(path)
    if not segments:
        segments = _fallback_asr_segments(duration_seconds)

    keyframe_step_ms = 15000 if family == "video" else 0
    results: list[PageResult] = []
    for idx, seg in enumerate(segments, start=1):
        text = str(seg.get("text", "")).strip()
        start_ms = int(seg.get("start_ms", 0))
        end_ms = int(seg.get("end_ms", start_ms))
        source_pointer = (
            f"timestamp:{start_ms}-{end_ms};keyframe_every_ms:{keyframe_step_ms}"
            if keyframe_step_ms > 0
            else f"timestamp:{start_ms}-{end_ms}"
        )
        untranscribed = "UNTRANSCRIBED" in text
        out = _write_page_text(settings, document_id, idx, text)
        results.append(
            PageResult(
                page_id=_page_id(document_id, idx),
                document_id=document_id,
                page_number=idx,
                source_tier="tier0_asr" if text and not untranscribed else "tier0_asr_placeholder",
                text_path=out,
                confidence=0.8 if text and not untranscribed else 0.1,
                hash_value=page_hash(text),
                page_type="text",
                source_pointer=source_pointer,
                timestamp_start_ms=start_ms,
                timestamp_end_ms=end_ms,
            )
        )
    return results


# ── Dispatch ──────────────────────────────────────────────────────────────────

def _process_document(settings: Settings, row) -> list[PageResult]:
    document_id = row["document_id"]
    path = Path(row["path"])
    family = row["doc_family"]

    if family == "pdf":
        return _extract_pdf_document(settings, document_id, path)
    if family in ("image", "screenshot"):
        return _extract_image_document(settings, document_id, path)
    if family == "text":
        return _extract_text_document(settings, document_id, path)
    if family in ("audio", "video"):
        return _extract_audio_or_video_document(settings, document_id, path, family)
    if family == "chat_export":
        return _extract_chat_export_document(settings, document_id, path)
    return []


# ── Main extract_text entry point ─────────────────────────────────────────────

def extract_text(settings: Settings, conn: Connection, workers: int = 4) -> int:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    llm_event_start = llm.call_event_count()
    stage_start = time.perf_counter()
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
    doc_confidence: dict[str, float] = {}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_to_doc: dict = {}
        future_started: dict = {}
        for row in rows:
            future = pool.submit(_process_document, settings, row)
            future_to_doc[future] = row["document_id"]
            future_started[future] = time.perf_counter()

        progress = tqdm(total=len(rows), desc="extract-text", unit="doc")
        for future in as_completed(future_to_doc):
            document_id = future_to_doc[future]
            try:
                page_results = future.result()
            except Exception:
                failed += 1
                progress.update(1)
                progress.set_postfix(inserted_pages=inserted_pages, skipped=skipped, failed=failed)
                continue

            doc_latency_ms = (time.perf_counter() - future_started[future]) * 1000.0
            confidences = [float(r.confidence) for r in page_results]
            avg_conf = mean(confidences) if confidences else 0.0
            doc_confidence[document_id] = avg_conf

            if page_results:
                for result in page_results:
                    tqdm.write(
                        "[extract-text] "
                        f"doc={result.document_id} page={result.page_number} "
                        f"ocr_conf={result.confidence:.3f} source={result.source_tier}"
                    )
            tqdm.write(
                "[extract-text] "
                f"doc={document_id} pages={len(page_results)} "
                f"doc_conf_avg={avg_conf:.3f} doc_elapsed_ms={doc_latency_ms:.1f}"
            )
            record_costly_call(
                settings,
                conn,
                stage="extract_text",
                step="document_parse",
                location="extract_text.extract_text",
                call_type="ocr_pipeline",
                duration_ms=doc_latency_ms,
                document_id=document_id,
                metadata={
                    "page_count": len(page_results),
                    "doc_conf_avg": round(avg_conf, 4),
                },
                success=True,
            )

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
                        fallback_trigger_reason, fallback_region, page_type
                    ) VALUES (%s, %s, %s, %s, %s, 'done', %s, %s, %s, %s, %s, %s, %s, %s)
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
                        result.page_type,
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
            progress.update(1)
            progress.set_postfix(
                inserted_pages=inserted_pages,
                skipped=skipped,
                failed=failed,
                last_doc_conf=f"{avg_conf:.3f}",
            )
        progress.close()

    after_in, after_out = llm.usage_snapshot()
    llm_events = llm.call_events_since(llm_event_start)
    for event in llm_events:
        tqdm.write(
            "[extract-text][llm] "
            f"method={event['method']} task={event['task']} "
            f"latency_ms={event['duration_ms']:.2f} "
            f"cache_hit={event['cache_hit']} success={event['success']}"
        )
        record_costly_call(
            settings,
            conn,
            stage="extract_text",
            step=str(event["task"]),
            location="extract_text.extract_text",
            call_type="llm_call",
            duration_ms=float(event["duration_ms"]),
            provider="openai",
            model_version=llm.model,
            cache_hit=bool(event["cache_hit"]),
            success=bool(event["success"]),
            metadata={"method": event["method"]},
        )
    if doc_confidence:
        overall_doc_conf = mean(doc_confidence.values())
        tqdm.write(
            "[extract-text] "
            f"documents={len(doc_confidence)} overall_doc_conf_avg={overall_doc_conf:.3f} "
            f"stage_elapsed_s={(time.perf_counter() - stage_start):.2f}"
        )

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
