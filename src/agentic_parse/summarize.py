from __future__ import annotations

from pathlib import Path

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_stage_metric
from .utils import atomic_write_text


def _build_context(chunks: list, max_chars: int = 18000) -> str:
    parts: list[str] = []
    total = 0
    for row in chunks:
        text = Path(row["text_path"]).read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        entry = f"[chunk_id:{row['chunk_id']} page:{row['page_number']}]\n{text}\n"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)
    return "\n".join(parts).strip()


def summarize(settings: Settings, conn: Connection) -> int:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    docs = conn.execute(
        """
        SELECT document_id
        FROM documents
        WHERE status_embed = 'done' AND summary_status != 'done'
        ORDER BY created_at ASC
        """
    ).fetchall()

    processed = 0
    skipped = 0
    for doc in docs:
        doc_id = doc["document_id"]
        chunks = retrieve_top_k_chunks(
            conn,
            "Summarize this document: key parties, dates, events, obligations, monetary amounts, and uncertainties.",
            top_k=24,
            max_chunks=48,
            max_tokens=9000,
            document_id=doc_id,
        )
        context = _build_context(chunks)
        if not context:
            document_summary = "No readable content."
        else:
            summary = llm.text(
                task="document_summary_from_embeddings",
                cache_dir=settings.llm_cache_dir,
                system_prompt=(
                    "You are a conservative analyst. Produce a concise factual summary using only provided chunks. "
                    "Do not infer beyond evidence. Explicitly mention uncertainty when information is incomplete."
                ),
                user_prompt=(
                    "Create a 5-8 sentence summary with this structure:\n"
                    "1) document type/purpose\n2) key entities\n3) key dates/timeline\n"
                    "4) financial or quantitative facts\n5) unresolved/uncertain points.\n\n"
                    f"Context:\n{context}"
                ),
                max_output_tokens=420,
            )
            document_summary = (summary or "No readable content.").strip()

        out = settings.summaries_dir / doc_id / "document.summary.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        new_value = document_summary.strip()
        if out.exists() and out.read_text(encoding="utf-8") == new_value:
            skipped += 1
        else:
            atomic_write_text(out, new_value)
            processed += 1
        conn.execute(
            "UPDATE documents SET summary_status = 'done', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (doc_id,),
        )

    after_in, after_out = llm.usage_snapshot()
    record_stage_metric(
        settings,
        conn,
        "summarize",
        processed=processed,
        skipped=skipped,
        failed=0,
        token_input=max(0, after_in - before_in),
        token_output=max(0, after_out - before_out),
    )
    conn.commit()
    return processed
