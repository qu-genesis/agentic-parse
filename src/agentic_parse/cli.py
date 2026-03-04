from __future__ import annotations

import argparse
import json
from pathlib import Path

from .chunk_embed import chunk_and_embed
from .config import Settings
from .db import connect, init_schema
from .entities import extract_entities, extract_for_query
from .extract_text import extract_text
from .ingest import ingest
from .paystub import extract_paystubs
from .summarize import summarize


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-parse",
        description="Scalable document ETL pipeline with provenance and incremental extraction.",
    )
    parser.add_argument("--workspace", type=Path, default=Path("./workspace"))
    parser.add_argument("--raw-root", type=Path, default=Path("./raw"))

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="Catalogue raw files and create immutable IDs.")

    p_extract = sub.add_parser("extract-text", help="Run tiered text extraction and cache per page.")
    p_extract.add_argument("--workers", type=int, default=4)

    sub.add_parser("chunk", help="Chunk page transcripts and compute embeddings.")
    sub.add_parser("summarize", help="Generate page/document summaries.")
    sub.add_parser("entities", help="Incrementally extract entities and relationships.")
    sub.add_parser("paystubs", help="Extract and validate pay-stub fields deterministically.")

    p_query = sub.add_parser("extract-query", help="Retrieval-first LLM extraction with hard caps.")
    p_query.add_argument("--query", required=True)
    p_query.add_argument("--top-k", type=int, default=8)
    p_query.add_argument("--max-chunks", type=int, default=20)
    p_query.add_argument("--max-tokens", type=int, default=6000)

    p_all = sub.add_parser("all", help="Run full pipeline end to end.")
    p_all.add_argument("--workers", type=int, default=4)

    sub.add_parser("status", help="Print stage status counts.")
    return parser


def _status(conn) -> str:
    doc_total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    done_ocr = conn.execute("SELECT COUNT(*) FROM documents WHERE status_ocr = 'done'").fetchone()[0]
    done_embed = conn.execute("SELECT COUNT(*) FROM documents WHERE status_embed = 'done'").fetchone()[0]
    done_entities = conn.execute("SELECT COUNT(*) FROM documents WHERE status_entities = 'done'").fetchone()[0]
    pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    rels = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    paystubs = conn.execute("SELECT COUNT(*) FROM paystubs").fetchone()[0]
    fallback_events = conn.execute("SELECT COUNT(*) FROM fallback_events").fetchone()[0]
    return (
        f"documents={doc_total} ocr_done={done_ocr} embed_done={done_embed} "
        f"entities_done={done_entities} pages={pages} chunks={chunks} "
        f"relationships={rels} paystubs={paystubs} fallback_events={fallback_events}"
    )


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    settings = Settings(workspace=args.workspace, raw_root=args.raw_root)
    settings.ensure_dirs()

    conn = connect(settings.db_path)
    init_schema(conn)

    if args.command == "ingest":
        count = ingest(settings, conn)
        print(f"ingested={count}")
        return

    if args.command == "extract-text":
        count = extract_text(settings, conn, workers=args.workers)
        print(f"pages_extracted={count}")
        return

    if args.command == "chunk":
        count = chunk_and_embed(settings, conn)
        print(f"chunks_inserted={count}")
        return

    if args.command == "summarize":
        count = summarize(settings, conn)
        print(f"pages_summarized={count}")
        return

    if args.command == "entities":
        entities, rels = extract_entities(settings, conn)
        print(f"entities_updated={entities} relationships_added={rels}")
        return

    if args.command == "paystubs":
        processed, needs_review = extract_paystubs(settings, conn)
        print(f"paystubs_processed={processed} needs_review={needs_review}")
        return

    if args.command == "extract-query":
        result = extract_for_query(
            settings,
            conn,
            query=args.query,
            top_k=args.top_k,
            max_chunks=args.max_chunks,
            max_tokens=args.max_tokens,
        )
        print(json.dumps(result, indent=2))
        return

    if args.command == "all":
        a = ingest(settings, conn)
        b = extract_text(settings, conn, workers=args.workers)
        c = chunk_and_embed(settings, conn)
        d = summarize(settings, conn)
        e, f = extract_entities(settings, conn)
        g, h = extract_paystubs(settings, conn)
        print(
            " ".join(
                [
                    f"ingested={a}",
                    f"pages_extracted={b}",
                    f"chunks_inserted={c}",
                    f"pages_summarized={d}",
                    f"entities_updated={e}",
                    f"relationships_added={f}",
                    f"paystubs_processed={g}",
                    f"paystubs_needs_review={h}",
                ]
            )
        )
        return

    if args.command == "status":
        print(_status(conn))
        return


if __name__ == "__main__":
    main()
