# Re-running the pipeline

The pipeline is idempotent — already-processed documents are skipped by default. Choose the option below based on how much you want to reset.

---

  ## Interactive (prompts for option)                                                      
  ./scripts/reset_pipeline.sh                                                      
                                                                                          
  ## Direct                                                                              
  ./scripts/reset_pipeline.sh a              # full reset                                 
  ./scripts/reset_pipeline.sh b              # extraction reset (same files, new logic)   
  ./scripts/reset_pipeline.sh c              # incremental, new files only                
                                                                                          
  ## With custom workers or paths                                                          
  ./scripts/reset_pipeline.sh b --workers 8                                               
  WORKSPACE=./my_workspace ./scripts/reset_pipeline.sh c                                  
                                                                                          
  Options A and B both prompt for confirmation before doing anything destructiv

## Option A — Full reset (re-run everything from scratch)

Wipes all derived state including ingest. Use this when you want a completely clean slate or have replaced/renamed source files.

```bash
# 1. Drop all pipeline tables (keeps the DB itself)
python3 -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:postgres@localhost:5432/agentic_parse')
cur = conn.cursor()
cur.execute('DROP TABLE IF EXISTS vector_index, chunks, pages, relationships, paystubs, fallback_events, stage_metrics, documents CASCADE')
conn.commit()
"

# 2. Delete derived workspace files
rm -rf workspace/derived workspace/outputs

# 3. Re-run the full pipeline
source .venv/bin/activate
python -m agentic_parse.cli --workspace ./workspace --raw-root ./data/raw all --workers 4
```

---

## Option B — Re-run extraction only (same files, test new OCR/extraction changes)

Resets text extraction and all downstream stages without re-hashing files. Use this when the source documents haven't changed but you want to re-process with updated extraction logic (e.g. new OCR improvements, quality scoring, page-type classification).

Preserves: the `documents` table rows, stable `document_id`s, and ingest metadata.
Resets: pages, chunks, embeddings, summaries, entities, fallback events.

```bash
# 1. Reset extraction state in DB
python3 -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:postgres@localhost:5432/agentic_parse')
cur = conn.cursor()
cur.execute('DELETE FROM vector_index')
cur.execute('DELETE FROM chunks')
cur.execute('DELETE FROM pages')
cur.execute('DELETE FROM fallback_events')
cur.execute(\"UPDATE documents SET status_ocr='pending', status_asr='pending', status_embed='pending', summary_status='pending_transcription'\")
conn.commit()
"

# 2. Delete derived workspace files
rm -rf workspace/derived

# 3. Re-run from extract-text onwards
source .venv/bin/activate
python -m agentic_parse.cli --workspace ./workspace --raw-root ./data/raw extract-text --workers 4
python -m agentic_parse.cli --workspace ./workspace --raw-root ./data/raw chunk
python -m agentic_parse.cli --workspace ./workspace --raw-root ./data/raw summarize
python -m agentic_parse.cli --workspace ./workspace --raw-root ./data/raw entities
```

---

## Option C — No reset (process new documents only)

Just run the pipeline normally. Documents already processed are skipped; only newly added files in `data/raw` are picked up.

```bash
source .venv/bin/activate
python -m agentic_parse.cli --workspace ./workspace --raw-root ./data/raw all --workers 4
```

---

## Quick reference

| Option | Use when | Preserves ingest | Wipes derived |
|--------|----------|-----------------|---------------|
| A | Clean slate / files replaced | No | Yes |
| B | Same files, new extraction logic | Yes | Yes |
| C | Adding new files only | Yes | No |
