"""
Sandbox tier comparison: runs Tier-0, Tier-1, and Tier-2 on every page of a PDF
and prints side-by-side results so you can evaluate extraction quality per tier.

Usage:
    uv run python scripts/tier_compare.py <path-to-pdf> [--pages 1-5]

Tier-2a (LLM cleanup) and Tier-2b (vision OCR) require OPENAI_API_KEY.
Tier-0 and Tier-1 are fully local.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import re
from pathlib import Path

# ── args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Compare tier 0/1/2 text extraction on a PDF.")
parser.add_argument("pdf", type=Path, help="Path to the PDF file")
parser.add_argument("--pages", default=None, help="Page range to test, e.g. '1-3' or '2'. Default: all.")
args = parser.parse_args()

PDF = args.pdf.resolve()
if not PDF.exists():
    sys.exit(f"File not found: {PDF}")

# ── page range ────────────────────────────────────────────────────────────────
def _parse_range(spec: str | None, total: int) -> list[int]:
    if spec is None:
        return list(range(1, total + 1))
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]

# ── shared lock (pypdfium2 / pytesseract are not thread-safe) ─────────────────
_LOCK = threading.Lock()

# ── quality scorer (copied from extract_text.py) ──────────────────────────────
_COMMON_WORD_RE = re.compile(r"\b[a-zA-Z]{3,}\b")
_NUMERIC_TOKEN_RE = re.compile(r"\b[\d,.$%]+\b")

def _quality(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    s = text.strip()
    total = len(s)
    if total < 30:
        return 0.1
    alpha = sum(c.isalpha() for c in s)
    printable = sum(c.isprintable() for c in s)
    unique_ratio = len(set(s)) / min(total, 200)
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    short_line_ratio = sum(1 for ln in lines if len(ln) <= 2) / len(lines) if lines else 1.0
    ws = sum(c.isspace() for c in text)
    ws_ratio = ws / max(1, len(text))
    word_count = len(_COMMON_WORD_RE.findall(s))
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

# ── Tier-0 backends ───────────────────────────────────────────────────────────

def tier0_pymupdf(path: Path, page: int) -> tuple[str, float]:
    try:
        import fitz
        doc = fitz.open(path.as_posix())
        text = doc[page - 1].get_text("text").strip()
        doc.close()
        return text, _quality(text)
    except Exception as e:
        return f"[ERROR: {e}]", 0.0

def tier0_pdfplumber(path: Path, page: int) -> tuple[str, float]:
    try:
        import pdfplumber
        with pdfplumber.open(path.as_posix()) as pdf:
            text = (pdf.pages[page - 1].extract_text() or "").strip()
        return text, _quality(text)
    except Exception as e:
        return f"[ERROR: {e}]", 0.0

def tier0_pypdf(path: Path, page: int) -> tuple[str, float]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path.as_posix())
        text = (reader.pages[page - 1].extract_text() or "").strip()
        return text, _quality(text)
    except Exception as e:
        return f"[ERROR: {e}]", 0.0

# ── Tier-1: Tesseract OCR ─────────────────────────────────────────────────────

def tier1_ocr(path: Path, page: int) -> tuple[str, float]:
    try:
        import pypdfium2 as pdfium
        import pytesseract
        from PIL import ImageFilter

        best_text, best_conf = "", 0.0
        for rotation in [0, 90, 180, 270]:
            with _LOCK:
                doc = pdfium.PdfDocument(path.as_posix())
                pil = doc[page - 1].render(scale=2.0, rotation=rotation).to_pil()
            pil = pil.convert("L").filter(ImageFilter.SHARPEN)
            with _LOCK:
                data = pytesseract.image_to_data(pil, output_type=pytesseract.Output.DICT)
            text = " ".join(t for t in data.get("text", []) if str(t).strip())
            conf_vals = [float(v) for v in data.get("conf", []) if float(v) >= 0]
            conf = (sum(conf_vals) / len(conf_vals) / 100.0) if conf_vals else 0.0
            if conf > best_conf:
                best_text, best_conf = text.strip(), conf
        return best_text, best_conf
    except Exception as e:
        return f"[ERROR: {e}]", 0.0

# ── Tier-2a: LLM text cleanup ─────────────────────────────────────────────────

def tier2a_llm_cleanup(noisy_text: str) -> tuple[str, float]:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return "[SKIPPED: no API key set]", 0.0
    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "You are a document text restorer for noisy OCR. "
                    "Fix OCR artifacts without changing meaning. "
                    "Output plain text only — no markdown, no explanations."
                )},
                {"role": "user", "content": (
                    f"Clean this OCR text:\n\n{noisy_text}\n\nReturn cleaned plain text only."
                )},
            ],
            max_tokens=1200,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text, 0.75
    except Exception as e:
        return f"[ERROR: {e}]", 0.0

# ── Tier-2b: Vision OCR ───────────────────────────────────────────────────────

def tier2b_vision_ocr(path: Path, page: int) -> tuple[str, float]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "[SKIPPED: OPENAI_API_KEY not set]", 0.0
    try:
        import base64, io
        import pypdfium2 as pdfium
        from openai import OpenAI

        with _LOCK:
            doc = pdfium.PdfDocument(path.as_posix())
            pil = doc[page - 1].render(scale=2.0).to_pil()

        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": (
                    "You are a precise page transcription tool. "
                    "Output only the transcribed text in reading order. "
                    "Do not summarize. Do not explain. Preserve numbers, dates, and structure exactly."
                )},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    {"type": "text", "text": "Transcribe all visible text from this page. Return only the transcription."},
                ]},
            ],
            max_tokens=1500,
        )
        text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        print(
            f"  [tier2b tokens] in={usage.prompt_tokens} out={usage.completion_tokens} "
            f"total={usage.total_tokens} "
            f"cost_usd=${usage.prompt_tokens/1e6*2.50 + usage.completion_tokens/1e6*10.00:.4f}"
        )
        return text, 0.82
    except Exception as e:
        return f"[ERROR: {e}]", 0.0

# ── Page count ────────────────────────────────────────────────────────────────

def get_page_count(path: Path) -> int:
    try:
        import fitz
        return len(fitz.open(path.as_posix()))
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        return len(PdfReader(path.as_posix()).pages)
    except Exception:
        pass
    return 1

# ── Formatting helpers ────────────────────────────────────────────────────────

SEP = "─" * 80

def print_tier(label: str, text: str, score: float, preview_chars: int = 1200) -> None:
    print(f"\n{'━'*80}")
    print(f"  {label}  |  quality_score={score:.3f}  |  chars={len(text)}")
    print(f"{'━'*80}")
    preview = text[:preview_chars]
    if len(text) > preview_chars:
        preview += f"\n... [{len(text) - preview_chars} more chars truncated]"
    print(preview or "[empty]")

# ── Main ──────────────────────────────────────────────────────────────────────

total_pages = get_page_count(PDF)
pages = _parse_range(args.pages, total_pages)
pages = [p for p in pages if 1 <= p <= total_pages]

print(f"\n{'='*80}")
print(f"  PDF: {PDF.name}")
print(f"  Total pages: {total_pages}  |  Testing pages: {pages}")
print(f"  OPENAI_API_KEY: {'SET' if os.getenv('OPENAI_API_KEY') else 'NOT SET (Tier-2 will be skipped)'}")
print(f"{'='*80}")

for page_num in pages:
    print(f"\n\n{'#'*80}")
    print(f"#  PAGE {page_num} of {total_pages}")
    print(f"{'#'*80}")

    # ── Tier-0 ────────────────────────────────────────────────────────────────
    t0_pymupdf_text, t0_pymupdf_score = tier0_pymupdf(PDF, page_num)
    t0_pdfplumber_text, t0_pdfplumber_score = tier0_pdfplumber(PDF, page_num)
    t0_pypdf_text, t0_pypdf_score = tier0_pypdf(PDF, page_num)

    # Best tier-0
    best_t0 = max(
        [("pymupdf", t0_pymupdf_text, t0_pymupdf_score),
         ("pdfplumber", t0_pdfplumber_text, t0_pdfplumber_score),
         ("pypdf", t0_pypdf_text, t0_pypdf_score)],
        key=lambda x: x[2],
    )

    print_tier("TIER-0a  pymupdf", t0_pymupdf_text, t0_pymupdf_score)
    print_tier("TIER-0b  pdfplumber", t0_pdfplumber_text, t0_pdfplumber_score)
    print_tier("TIER-0c  pypdf", t0_pypdf_text, t0_pypdf_score)
    print(f"\n  ► Best Tier-0: {best_t0[0]}  (score={best_t0[2]:.3f})")

    # ── Tier-1 ────────────────────────────────────────────────────────────────
    print(f"\n{SEP}\nRunning Tier-1 OCR (Tesseract)...")
    t1_text, t1_conf = tier1_ocr(PDF, page_num)
    print_tier("TIER-1  tesseract_ocr", t1_text, t1_conf)

    # ── Tier-2 (only if needed or forced) ────────────────────────────────────
    # Feed Tier-2a the best text seen so far (Tier-0 or Tier-1, whichever scored higher)
    best_input = max(
        [("tier0:" + best_t0[0], best_t0[1], best_t0[2]),
         ("tier1:tesseract",      t1_text,    t1_conf)],
        key=lambda x: x[2],
    )
    print(f"\n{SEP}\nRunning Tier-2a LLM cleanup (input: {best_input[0]}, score={best_input[2]:.3f})...")
    t2a_text, t2a_conf = tier2a_llm_cleanup(best_input[1])
    print_tier("TIER-2a  llm_cleanup", t2a_text, t2a_conf)

    print(f"\n{SEP}\nRunning Tier-2b Vision OCR...")
    t2b_text, t2b_conf = tier2b_vision_ocr(PDF, page_num)
    print_tier("TIER-2b  vision_ocr", t2b_text, t2b_conf)

    # ── Summary for this page ─────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  PAGE {page_num} SUMMARY")
    print(f"{'='*80}")
    rows = [
        ("Tier-0a pymupdf",     t0_pymupdf_score,   len(t0_pymupdf_text)),
        ("Tier-0b pdfplumber",  t0_pdfplumber_score, len(t0_pdfplumber_text)),
        ("Tier-0c pypdf",       t0_pypdf_score,     len(t0_pypdf_text)),
        ("Tier-1  tesseract",   t1_conf,             len(t1_text)),
        ("Tier-2a llm_cleanup", t2a_conf,            len(t2a_text)),
        ("Tier-2b vision_ocr",  t2b_conf,            len(t2b_text)),
    ]
    for name, score, chars in rows:
        bar = "█" * int(score * 20)
        print(f"  {name:<25}  score={score:.3f}  chars={chars:5d}  [{bar:<20}]")

print(f"\n\n{'='*80}")
print("  DONE")
print(f"{'='*80}\n")
