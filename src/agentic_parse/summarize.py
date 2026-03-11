from __future__ import annotations

import json
from pathlib import Path
import time

from tqdm import tqdm

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .document_catalogue import build_document_catalogue
from .llm import get_llm_client
from .telemetry import record_costly_call, record_stage_metric
from .utils import atomic_write_text

# Documents with more pages than this get the segmented summarization path.
_SEGMENT_PAGE_THRESHOLD = 20
_SEGMENT_SIMILARITY_THRESHOLD = 0.35  # Jaccard drop below this → new segment


def _format_segment_json_for_toc(payload: dict) -> str:
    """Convert a segment-summary JSON dict to a compact text string for use in the composed-summary TOC."""
    parts = []
    if wc := payload.get("what_this_segment_contains"):
        parts.append(wc.strip())
    doc_types = payload.get("likely_document_types") or []
    if doc_types:
        parts.append(f"Types: {', '.join(doc_types)}")
    entities = payload.get("key_entities") or []
    if entities:
        parts.append(f"Entities: {', '.join(entities[:5])}")
    dates = payload.get("key_dates") or []
    if dates:
        parts.append(f"Dates: {', '.join(dates[:3])}")
    amounts = payload.get("key_amounts") or []
    if amounts:
        parts.append(f"Amounts: {', '.join(amounts[:3])}")
    uncertainties = payload.get("uncertainties") or []
    if uncertainties:
        parts.append(f"Uncertainties: {'; '.join(uncertainties[:2])}")
    return " | ".join(parts) if parts else ""


# ── Catalogue refresh ─────────────────────────────────────────────────────────

def _refresh_catalogue_jsonl(settings: Settings, conn: Connection) -> None:
    """Rewrite document_catalogue.jsonl from current DB state so it reflects live statuses."""
    rows = conn.execute(
        """
        SELECT document_id, sha256, path, media_type, doc_family, size_bytes,
               page_count, has_text_layer, audio_duration_seconds,
               video_duration_seconds, summary_status
        FROM documents
        ORDER BY created_at ASC
        """
    ).fetchall()
    lines = [
        json.dumps({
            "document_id": row["document_id"],
            "sha256": row["sha256"],
            "path": row["path"],
            "media_type": row["media_type"],
            "doc_family": row["doc_family"],
            "size_bytes": row["size_bytes"],
            "page_count": row["page_count"],
            "has_text_layer": None if row["has_text_layer"] is None else bool(row["has_text_layer"]),
            "audio_duration_seconds": row["audio_duration_seconds"],
            "video_duration_seconds": row["video_duration_seconds"],
            "summary_status": row["summary_status"],
        })
        for row in rows
    ]
    settings.catalogue_jsonl.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


def _write_grouped_catalogue(settings: Settings, conn: Connection, llm) -> tuple[int, int]:
    """Build and persist grouped document catalogue from short summaries."""
    rows = conn.execute(
        """
        SELECT document_id, path, doc_family, media_type, page_count
        FROM documents
        WHERE summary_status = 'done'
        ORDER BY created_at ASC
        """
    ).fetchall()

    docs: list[dict] = []
    for row in rows:
        summary_path = settings.summaries_dir / row["document_id"] / "document.summary.txt"
        if not summary_path.exists():
            continue
        text = summary_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        docs.append(
            {
                "document_id": row["document_id"],
                "name": Path(row["path"]).name,
                "doc_family": row["doc_family"],
                "media_type": row["media_type"],
                "page_count": row["page_count"],
                "summary_text": text,
            }
        )

    payload = build_document_catalogue(
        documents=docs,
        llm=llm,
        cache_dir=settings.llm_cache_dir,
    )
    atomic_write_text(settings.grouped_catalogue_json, json.dumps(payload, indent=2))
    return int(payload.get("document_count", 0)), int(payload.get("group_count", 0))


# ── Context building ──────────────────────────────────────────────────────────

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


# ── Step 6: Long-PDF segmentation ────────────────────────────────────────────

def _page_word_set(text_path: str) -> set[str]:
    """Return lowercase words of length >= 3 from a page text file."""
    try:
        text = Path(text_path).read_text(encoding="utf-8", errors="ignore")
        return {w.lower() for w in text.split() if len(w) >= 3 and w.isalpha()}
    except Exception:
        return set()


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _detect_segments(pages: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """
    Split a page list into thematic segments using adjacent Jaccard similarity.

    pages: [(page_number, text_path), ...]
    Returns a list of segments, each a list of (page_number, text_path).
    """
    if len(pages) <= 1:
        return [pages]

    word_sets = [_page_word_set(tp) for _, tp in pages]
    segments: list[list[tuple[int, str]]] = [[pages[0]]]

    for i in range(1, len(pages)):
        sim = _jaccard(word_sets[i - 1], word_sets[i])
        if sim < _SEGMENT_SIMILARITY_THRESHOLD:
            segments.append([])
        segments[-1].append(pages[i])

    # Merge tiny segments (< 3 pages) into adjacent ones to avoid over-fragmentation
    merged: list[list[tuple[int, str]]] = []
    for seg in segments:
        if merged and len(seg) < 3:
            merged[-1].extend(seg)
        else:
            merged.append(seg)
    return merged


def _summarize_segment(
    llm,
    settings: Settings,
    conn: Connection,
    doc_id: str,
    segment: list[tuple[int, str]],
    seg_index: int,
    total_segments: int,
) -> str:
    """Summarize a single page segment using its chunks."""
    page_numbers = [pn for pn, _ in segment]
    first_page, last_page = page_numbers[0], page_numbers[-1]

    # Pull chunks only for pages in this segment
    chunk_rows = conn.execute(
        """
        SELECT c.chunk_id, c.page_number, c.text_path, c.token_estimate
        FROM chunks c
        WHERE c.document_id = %s AND c.page_number >= %s AND c.page_number <= %s
        ORDER BY c.page_number, c.chunk_index
        """,
        (doc_id, first_page, last_page),
    ).fetchall()

    context = _build_context(chunk_rows, max_chars=12000)
    if not context:
        # Fall back to reading text files directly
        parts = []
        for _, tp in segment[:10]:  # cap at 10 pages for safety
            try:
                t = Path(tp).read_text(encoding="utf-8", errors="ignore").strip()
                if t:
                    parts.append(t[:1200])
            except Exception:
                continue
        context = "\n\n".join(parts)

    if not context:
        return f"[Segment {seg_index + 1} of {total_segments}, pages {first_page}–{last_page}: no readable content]"

    payload = llm.json(
        task="segment_summary",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "You are a conservative, evidence-only analyst. "
            "Summarize only what is directly supported by the provided text. "
            "Do not infer, speculate, or fill gaps. "
            "If the text appears truncated or incomplete, explicitly say so in the uncertainties field. "
            "Return output as strict JSON only matching the requested schema."
        ),
        user_prompt=(
            f"Summarize pages {first_page}–{last_page} "
            f"(segment {seg_index + 1} of {total_segments}) from a composite PDF where format "
            "can change abruptly between pages (letter → invoice → table → handwritten note).\n"
            "The text below may be incomplete or truncated per page.\n\n"
            "Output JSON schema:\n"
            "{\n"
            f'  "segment_range": "{first_page}-{last_page}",\n'
            '  "likely_document_types": ["one or more of: invoice, receipt, letter, report, table, form, handwritten_note, comment_thread, mixed, unknown"],\n'
            '  "what_this_segment_contains": "2-6 factual bullet-like sentences in one string",\n'
            '  "key_entities": ["names/orgs/addresses if explicit"],\n'
            '  "key_dates": ["dates if explicit"],\n'
            '  "key_amounts": ["amounts/totals if explicit"],\n'
            '  "uncertainties": ["missing/illegible/truncated/contradictory cues"]\n'
            "}\n\n"
            f"SEGMENT TEXT:\n{context}\n\n"
            "Return JSON only."
        ),
        max_output_tokens=400,
    )
    if payload:
        formatted = _format_segment_json_for_toc(payload)
        if formatted:
            return formatted
    return f"[Segment {seg_index + 1}: could not summarize]"


def _segmented_summary(
    llm,
    settings: Settings,
    conn: Connection,
    doc_id: str,
) -> str:
    """
    Step 6: For long PDFs, segment by Jaccard change-points, summarize each
    segment, then compose a document-level summary with a section index.
    """
    page_rows = conn.execute(
        """
        SELECT p.page_number, p.text_path, p.page_type
        FROM pages p
        WHERE p.document_id = %s
        ORDER BY p.page_number
        """,
        (doc_id,),
    ).fetchall()

    if not page_rows:
        return "No readable content."

    pages = [(r["page_number"], r["text_path"]) for r in page_rows]
    segments = _detect_segments(pages)

    segment_summaries: list[str] = []
    for i, seg in enumerate(segments):
        summary = _summarize_segment(llm, settings, conn, doc_id, seg, i, len(segments))
        page_numbers = [pn for pn, _ in seg]
        # Get dominant page type for this segment
        seg_types = [
            r["page_type"] for r in page_rows
            if r["page_number"] in set(page_numbers)
        ]
        type_label = max(set(seg_types), key=seg_types.count) if seg_types else "text"
        segment_summaries.append(
            f"Section {i + 1} (pages {page_numbers[0]}–{page_numbers[-1]}, type: {type_label}): {summary}"
        )

    toc = "\n".join(segment_summaries)

    # Compose overall summary
    composed = llm.json(
        task="segmented_document_summary",
        cache_dir=settings.llm_cache_dir,
        system_prompt=(
            "You are a conservative, evidence-only analyst composing a document-level summary "
            "from section summaries.\n"
            "Rules:\n"
            "- Use only the provided section summaries; do not infer missing details.\n"
            "- If section summaries conflict (e.g., different totals), report the conflict in "
            "'uncertainties' rather than resolving it.\n"
            "- Return strict JSON only."
        ),
        user_prompt=(
            "You are given section summaries of a long, stitched PDF "
            "(multiple sub-documents in one file). Create a document-level summary.\n\n"
            "Output JSON schema:\n"
            "{\n"
            '  "overall_purpose": "1-3 sentences; evidence-only",\n'
            '  "document_types_present": ["invoice|receipt|letter|report|table|form|handwritten_note|comment_thread|mixed|unknown"],\n'
            '  "key_entities": ["explicit entities across sections"],\n'
            '  "timeline": ["ordered key dates/events if explicit; otherwise empty"],\n'
            '  "financial_facts": ["explicit totals/amounts/currencies if present"],\n'
            '  "table_of_contents": [\n'
            '    {"section_index": 1, "pages_or_range": "if provided", "one_sentence_summary": "evidence-only"}\n'
            "  ],\n"
            '  "uncertainties": ["conflicts, missing detail, unclear doc boundaries, truncated cues"]\n'
            "}\n\n"
            f"SECTION SUMMARIES:\n{toc}\n\n"
            "Return JSON only."
        ),
        # Scale token budget with segment count: ~60 tokens per TOC entry + 300 base
        max_output_tokens=min(1400, 300 + 60 * len(segments)),
    )

    if composed:
        composed_text = json.dumps(composed, indent=2)
        return composed_text + f"\n\n---\nSections detected: {len(segments)}\n{toc}"
    return toc


# ── Main summarize entry point ────────────────────────────────────────────────

def summarize(settings: Settings, conn: Connection) -> int:
    llm = get_llm_client()
    before_in, before_out = llm.usage_snapshot()
    llm_event_start = llm.call_event_count()
    stage_start = time.perf_counter()
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
    progress = tqdm(docs, total=len(docs), desc="summarize", unit="doc")
    for doc in progress:
        doc_start = time.perf_counter()
        doc_llm_event_start = llm.call_event_count()
        doc_id = doc["document_id"]

        has_chunks = conn.execute(
            "SELECT 1 FROM chunks WHERE document_id = %s LIMIT 1", (doc_id,)
        ).fetchone()

        if not has_chunks:
            document_summary = "No readable content."
        else:
            # Step 6: check page count to decide between flat vs segmented summary
            page_count = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE document_id = %s", (doc_id,)
            ).fetchone()[0]

            if page_count >= _SEGMENT_PAGE_THRESHOLD:
                document_summary = _segmented_summary(llm, settings, conn, doc_id)
            else:
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
                    summary_payload = llm.json(
                        task="document_summary_from_embeddings",
                        cache_dir=settings.llm_cache_dir,
                        system_prompt=(
                            "You are a conservative, retrieval-grounded summarizer. "
                            "Use only the provided context chunks. "
                            "Do not infer beyond evidence. "
                            "If information is incomplete, say so explicitly. "
                            "Return strict JSON only."
                        ),
                        user_prompt=(
                            "The following are retrieved text chunks from a stitched PDF that may mix "
                            "invoices/receipts/tables/forms/letters/reports/handwriting. "
                            "Chunks may omit pages, tables may lose alignment, and some sections may be missing.\n\n"
                            "Create a concise summary with this JSON structure:\n"
                            "{\n"
                            '  "document_type_or_mix": "invoice|receipt|letter|report|table|form|handwritten_note|comment_thread|mixed|unknown",\n'
                            '  "purpose": "1-2 sentences",\n'
                            '  "key_entities": ["explicit parties/orgs"],\n'
                            '  "key_dates": ["explicit dates"],\n'
                            '  "quantitative_facts": ["explicit amounts, totals, quantities, invoice numbers"],\n'
                            '  "uncertainties": ["what is missing/ambiguous/conflicting due to retrieval or truncation"]\n'
                            "}\n\n"
                            f"CONTEXT CHUNKS:\n{context}\n\n"
                            "Return JSON only."
                        ),
                        max_output_tokens=500,
                    )
                    if summary_payload:
                        document_summary = json.dumps(summary_payload, indent=2)
                    else:
                        document_summary = "No readable content."

        out = settings.summaries_dir / doc_id / "document.summary.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        new_value = document_summary.strip()
        summary_changed = not (out.exists() and out.read_text(encoding="utf-8") == new_value)
        if not summary_changed:
            skipped += 1
        else:
            atomic_write_text(out, new_value)
            processed += 1
        conn.execute(
            "UPDATE documents SET summary_status = 'done', updated_at = CURRENT_TIMESTAMP WHERE document_id = %s",
            (doc_id,),
        )
        doc_llm_events = llm.call_events_since(doc_llm_event_start)
        doc_llm_ms = sum(float(e["duration_ms"]) for e in doc_llm_events)
        for event in doc_llm_events:
            tqdm.write(
                "[summarize][llm] "
                f"doc={doc_id} method={event['method']} task={event['task']} "
                f"latency_ms={event['duration_ms']:.2f} "
                f"cache_hit={event['cache_hit']} success={event['success']}"
            )
            record_costly_call(
                settings,
                conn,
                stage="summarize",
                step=str(event["task"]),
                location="summarize.summarize",
                call_type="llm_call",
                duration_ms=float(event["duration_ms"]),
                document_id=doc_id,
                provider="openai",
                model_version=llm.model,
                cache_hit=bool(event["cache_hit"]),
                success=bool(event["success"]),
                metadata={"method": event["method"]},
            )
        record_costly_call(
            settings,
            conn,
            stage="summarize",
            step="document_summary",
            location="summarize.summarize",
            call_type="doc_operation",
            duration_ms=(time.perf_counter() - doc_start) * 1000.0,
            document_id=doc_id,
            metadata={"changed": summary_changed, "llm_calls": len(doc_llm_events)},
            success=True,
        )
        tqdm.write(
            "[summarize] "
            f"doc={doc_id} changed={summary_changed} "
            f"llm_calls={len(doc_llm_events)} llm_total_ms={doc_llm_ms:.2f} "
            f"doc_elapsed_ms={(time.perf_counter() - doc_start) * 1000.0:.1f}"
        )
        progress.set_postfix(processed=processed, skipped=skipped)
    progress.close()

    catalogue_start = time.perf_counter()
    catalogue_llm_event_start = llm.call_event_count()
    grouped_doc_count, grouped_group_count = _write_grouped_catalogue(settings, conn, llm)
    catalogue_events = llm.call_events_since(catalogue_llm_event_start)
    for event in catalogue_events:
        tqdm.write(
            "[summarize][catalogue][llm] "
            f"method={event['method']} task={event['task']} "
            f"latency_ms={event['duration_ms']:.2f} "
            f"cache_hit={event['cache_hit']} success={event['success']}"
        )
        record_costly_call(
            settings,
            conn,
            stage="summarize",
            step=str(event["task"]),
            location="summarize._write_grouped_catalogue",
            call_type="llm_call",
            duration_ms=float(event["duration_ms"]),
            provider="openai",
            model_version=llm.model,
            cache_hit=bool(event["cache_hit"]),
            success=bool(event["success"]),
            metadata={"method": event["method"], "operation": "catalogue_grouping"},
        )
    record_costly_call(
        settings,
        conn,
        stage="summarize",
        step="document_catalogue_grouping",
        location="summarize._write_grouped_catalogue",
        call_type="doc_operation",
        duration_ms=(time.perf_counter() - catalogue_start) * 1000.0,
        metadata={
            "document_count": grouped_doc_count,
            "group_count": grouped_group_count,
            "llm_calls": len(catalogue_events),
        },
        success=True,
    )

    after_in, after_out = llm.usage_snapshot()
    llm_events = llm.call_events_since(llm_event_start)
    tqdm.write(
        "[summarize] "
        f"documents={len(docs)} llm_calls={len(llm_events)} "
        f"catalogue_docs={grouped_doc_count} catalogue_groups={grouped_group_count} "
        f"llm_total_ms={sum(float(e['duration_ms']) for e in llm_events):.2f} "
        f"elapsed_s={(time.perf_counter() - stage_start):.2f}"
    )
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
    _refresh_catalogue_jsonl(settings, conn)
    conn.commit()
    return processed
