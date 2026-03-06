#!/usr/bin/env bash
# reset_pipeline.sh — Reset and re-run the agentic-parse pipeline.
#
# Usage:
#   ./scripts/reset_pipeline.sh [a|b|c] [--workers N]
#
# Options:
#   a  Full reset — drop all DB tables and derived files, re-run everything.
#   b  Extraction reset — keep ingest, wipe pages/chunks/embeddings, re-run from extract-text.
#   c  Incremental — no reset, process new files only.
#
# Flags:
#   --workers N   Number of parallel workers for extract-text (default: 4).

set -euo pipefail

WORKSPACE="${WORKSPACE:-./workspace}"
RAW_ROOT="${RAW_ROOT:-./data/raw}"
DB_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/agentic_parse}"
WORKERS=4

# ── Pre-flight checks ─────────────────────────────────────────────────────────

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "ERROR: OPENAI_API_KEY is not set."
    echo "       Export it before running: export OPENAI_API_KEY=<your-key>"
    exit 1
fi

# ── Argument parsing ──────────────────────────────────────────────────────────

OPTION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        a|b|c) OPTION="$1"; shift ;;
        --workers) WORKERS="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 [a|b|c] [--workers N]"
            exit 1
            ;;
    esac
done

if [[ -z "$OPTION" ]]; then
    echo "Select a reset option:"
    echo "  a  Full reset     — drop all DB tables + derived files, re-run everything"
    echo "  b  Extract reset  — keep ingest, wipe pages/chunks/embeddings, re-run from extract-text"
    echo "  c  Incremental    — no reset, process new files only"
    echo ""
    read -rp "Enter option [a/b/c]: " OPTION
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

activate_venv() {
    if [[ -f ".venv/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate
    fi
}

run_pipeline() {
    local from_stage="${1:-all}"
    activate_venv
    if [[ "$from_stage" == "all" ]]; then
        python -m agentic_parse.cli --workspace "$WORKSPACE" --raw-root "$RAW_ROOT" all --workers "$WORKERS"
    else
        python -m agentic_parse.cli --workspace "$WORKSPACE" --raw-root "$RAW_ROOT" extract-text --workers "$WORKERS"
        python -m agentic_parse.cli --workspace "$WORKSPACE" --raw-root "$RAW_ROOT" chunk
        python -m agentic_parse.cli --workspace "$WORKSPACE" --raw-root "$RAW_ROOT" summarize
        python -m agentic_parse.cli --workspace "$WORKSPACE" --raw-root "$RAW_ROOT" entities
    fi
}

# ── Option A ──────────────────────────────────────────────────────────────────

option_a() {
    echo "==> Option A: Full reset"
    echo "    This will drop all pipeline tables and delete all derived workspace files."
    read -rp "    Confirm? [y/N]: " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

    echo "--> Dropping DB tables..."
    python3 - <<PYEOF
import psycopg2
conn = psycopg2.connect("$DB_URL")
cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS vector_index, chunks, pages, relationships, paystubs, fallback_events, stage_metrics, documents CASCADE")
conn.commit()
conn.close()
print("    Tables dropped.")
PYEOF

    echo "--> Deleting derived workspace files..."
    rm -rf "$WORKSPACE/derived" "$WORKSPACE/outputs"
    echo "    Done."

    echo "--> Running full pipeline..."
    run_pipeline all
}

# ── Option B ──────────────────────────────────────────────────────────────────

option_b() {
    echo "==> Option B: Extraction reset (keeps ingest)"
    echo "    This will wipe pages, chunks, embeddings, summaries, and entities."
    echo "    Ingest metadata (document_id, sha256, path) is preserved."
    read -rp "    Confirm? [y/N]: " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

    echo "--> Resetting extraction state in DB..."
    python3 - <<PYEOF
import psycopg2
conn = psycopg2.connect("$DB_URL")
cur = conn.cursor()
cur.execute("DELETE FROM vector_index")
cur.execute("DELETE FROM chunks")
cur.execute("DELETE FROM pages")
cur.execute("DELETE FROM fallback_events")
cur.execute("UPDATE documents SET status_ocr='pending', status_asr='pending', status_embed='pending', summary_status='pending_transcription'")
conn.commit()
conn.close()
print("    DB reset complete.")
PYEOF

    echo "--> Deleting derived workspace files..."
    rm -rf "$WORKSPACE/derived"
    echo "    Done."

    echo "--> Running pipeline from extract-text..."
    run_pipeline extract-text
}

# ── Option C ──────────────────────────────────────────────────────────────────

option_c() {
    echo "==> Option C: Incremental run (no reset)"
    echo "    Only new files in $RAW_ROOT will be processed."
    echo "--> Running pipeline..."
    run_pipeline all
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "$OPTION" in
    a) option_a ;;
    b) option_b ;;
    c) option_c ;;
    *)
        echo "Invalid option: $OPTION. Choose a, b, or c."
        exit 1
        ;;
esac

echo ""
echo "==> Done."
