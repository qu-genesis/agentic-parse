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
    sub.add_parser("paystubs", help="Extract and validate payment records using embedding retrieval + LLM.")

    p_query = sub.add_parser("extract-query", help="Retrieval-first LLM extraction with hard caps.")
    p_query.add_argument("--query", required=True)
    p_query.add_argument("--top-k", type=int, default=8)
    p_query.add_argument("--max-chunks", type=int, default=20)
    p_query.add_argument("--max-tokens", type=int, default=6000)

    p_cost = sub.add_parser("cost-report", help="Show costly-step hotspots for scaling analysis.")
    p_cost.add_argument("--top", type=int, default=20)
    p_cost.add_argument("--stage", default=None)

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
    costly_calls = conn.execute("SELECT COUNT(*) FROM costly_calls").fetchone()[0]
    return (
        f"documents={doc_total} ocr_done={done_ocr} embed_done={done_embed} "
        f"entities_done={done_entities} pages={pages} chunks={chunks} "
        f"relationships={rels} paystubs={paystubs} fallback_events={fallback_events} "
        f"costly_calls={costly_calls}"
    )


def _cost_report(conn, *, top: int, stage: str | None) -> str:
    rows = conn.execute(
        """
        SELECT
            stage,
            step,
            call_type,
            location,
            COUNT(*) AS calls,
            SUM(duration_ms) AS total_ms,
            AVG(duration_ms) AS avg_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
            SUM(CASE WHEN cache_hit IS TRUE THEN 1 ELSE 0 END) AS cache_hits,
            SUM(CASE WHEN success IS TRUE THEN 1 ELSE 0 END) AS successes
        FROM costly_calls
        WHERE (%s IS NULL OR stage = %s)
        GROUP BY stage, step, call_type, location
        ORDER BY total_ms DESC
        LIMIT %s
        """,
        (stage, stage, max(1, top)),
    ).fetchall()

    if not rows:
        return "cost-report: no costly_calls rows found"

    stage_rows = conn.execute(
        """
        SELECT
            stage,
            COUNT(*) AS calls,
            SUM(duration_ms) AS total_ms,
            AVG(duration_ms) AS avg_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
        FROM costly_calls
        WHERE (%s IS NULL OR stage = %s)
        GROUP BY stage
        ORDER BY total_ms DESC
        """,
        (stage, stage),
    ).fetchall()

    lines = []
    lines.append("=== Costly Call Hotspots ===")
    lines.append("stage | step | call_type | location | calls | total_ms | avg_ms | p95_ms | cache_hit_rate | success_rate")
    for row in rows:
        calls = int(row["calls"] or 0)
        cache_hits = int(row["cache_hits"] or 0)
        successes = int(row["successes"] or 0)
        cache_rate = (cache_hits / calls * 100.0) if calls else 0.0
        success_rate = (successes / calls * 100.0) if calls else 0.0
        lines.append(
            " | ".join(
                [
                    str(row["stage"]),
                    str(row["step"]),
                    str(row["call_type"]),
                    str(row["location"]),
                    str(calls),
                    f"{float(row['total_ms'] or 0.0):.2f}",
                    f"{float(row['avg_ms'] or 0.0):.2f}",
                    f"{float(row['p95_ms'] or 0.0):.2f}",
                    f"{cache_rate:.1f}%",
                    f"{success_rate:.1f}%",
                ]
            )
        )

    lines.append("")
    lines.append("=== Stage Scaling Summary ===")
    lines.append("stage | calls | total_ms | avg_ms | p95_ms")
    for row in stage_rows:
        lines.append(
            " | ".join(
                [
                    str(row["stage"]),
                    str(int(row["calls"] or 0)),
                    f"{float(row['total_ms'] or 0.0):.2f}",
                    f"{float(row['avg_ms'] or 0.0):.2f}",
                    f"{float(row['p95_ms'] or 0.0):.2f}",
                ]
            )
        )
    return "\n".join(lines)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    settings = Settings(workspace=args.workspace, raw_root=args.raw_root)
    settings.ensure_dirs()

    conn = connect(settings.db_dsn)
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

    if args.command == "cost-report":
        print(_cost_report(conn, top=args.top, stage=args.stage))
        return

    if args.command == "status":
        print(_status(conn))
        return


if __name__ == "__main__":
    main()
