from __future__ import annotations

from pathlib import Path
import sqlite3

from .config import Settings
from .llm import get_llm_client
from .telemetry import record_stage_metric
from .utils import atomic_write_text


def _first_sentence(text: str, limit: int = 240) -> str:
    clean = " ".join(text.split())
    if not clean:
        return ""
    for sep in ".!?":
        pos = clean.find(sep)
        if 0 < pos < limit:
            return clean[: pos + 1]
    return clean[:limit]


def summarize(settings: Settings, conn: sqlite3.Connection) -> int:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    rows = conn.execute(
        "SELECT page_id, document_id, page_number, text_path FROM pages ORDER BY document_id, page_number"
    ).fetchall()

    writes = 0
    skipped = 0
    per_doc: dict[str, list[str]] = {}
    for row in rows:
        text_path = Path(row["text_path"])
        if not text_path.exists():
            continue
        text = text_path.read_text(encoding="utf-8")
        out = settings.summaries_dir / row["document_id"] / f"page_{row['page_number']:04d}.summary.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            summary = out.read_text(encoding="utf-8")
            skipped += 1
        else:
            summary = llm.text(
                task="page_summary",
                cache_dir=settings.llm_cache_dir,
                system_prompt=(
                    "You summarize investigative documents conservatively. "
                    "Keep uncertainty, never infer missing facts."
                ),
                user_prompt=(
                    "Summarize this page in 1-2 sentences.\n"
                    "If text is empty/illegible, return: 'No readable content.'\n\n"
                    f"{text}"
                ),
                max_output_tokens=180,
            )
            if summary is None:
                summary = _first_sentence(text)
            summary = summary.strip()
            atomic_write_text(out, summary)
            writes += 1
        per_doc.setdefault(row["document_id"], []).append(summary)

    for doc_id, summaries in per_doc.items():
        document_summary = " ".join([s for s in summaries if s][:8])
        out = settings.summaries_dir / doc_id / "document.summary.txt"
        new_value = document_summary.strip()
        if out.exists() and out.read_text(encoding="utf-8") == new_value:
            skipped += 1
            continue
        atomic_write_text(out, new_value)
        writes += 1
        conn.execute(
            "UPDATE documents SET summary_status = 'done', updated_at = CURRENT_TIMESTAMP WHERE document_id = ?",
            (doc_id,),
        )

    after_in, after_out = llm.usage_snapshot()
    record_stage_metric(
        settings,
        conn,
        "summarize",
        processed=writes,
        skipped=skipped,
        failed=0,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
    )
    conn.commit()
    return writes
