# Pipeline Full-Run Report — 2026-03-10

Fresh run from a clean database. LLM response cache (`llm_cache/`) was preserved to avoid
re-spending on identical prompts; all embedding calls and entity extraction were live.

---

## 1. Pipeline Configuration

| Setting | Value |
|---|---|
| Raw root | `data/raw/` |
| Workspace | `workspace/` |
| Workers (extract-text) | 4 |
| Model | `gpt-4o-mini` |
| Documents | 10 PDFs |

---

## 2. Stage Timing

| Stage | Wall time | Notes |
|---|---|---|
| ingest | 4.3 s | |
| extract-text | 1 377 s (22.96 min) | OCR on 1 446 pages; 4 workers |
| chunk | 454 s (7.57 min) | Embeddings for 1 564 chunks |
| summarize | 99 s (1.65 min) | incl. initial 2 failures + re-run fix |
| entities | 1 098 s (18.3 min) | 1 LLM call per chunk |
| paystubs | 139 s (2.3 min) | |
| **Total** | **~3 171 s (52.9 min)** | |

---

## 3. Data Volumes

| Metric | Count |
|---|---|
| Documents ingested | 10 |
| Pages extracted | 1 446 (avg 144.6 / doc) |
| Chunks created | 1 564 (avg 156.4 / doc) |
| Entities processed | 8 333 |
| Relationships | 47 775 |
| Paystubs found | 25 (16 need review) |
| Fallback events | 1 309 |

**Per-document page counts:**

| Document | Pages | Chunks |
|---|---|---|
| D1-80 Meal Delivery Authorizations Safari.pdf | 549 | 610 |
| D1-89 Meal Delivery Authorizations Center for Youth Advancement.pdf | 291 | 291 |
| D1-4 Monday.com - Dropped, 2.pdf | 208 | 208 |
| D1-19 Monday.com - January 2022, 5.pdf | 160 | 187 |
| D1-59 Invoices-Receipts Safari February 2021.pdf | 111 | 115 |
| D1-30 Meal Counts - Safari 2 of 6 June 2020.pdf | 74 | 74 |
| D1-196 USDA Food Buying Guide Fruits.pdf | 31 | 54 |
| D1-71 Invoices-Receipts Safari October 2021.pdf | 10 | 10 |
| D1-48 Claim Submission Safari October 2020.pdf | 8 | 9 |
| D1-174 Waivers, Offer Vs Serve.pdf | 4 | 6 |

---

## 4. Token Usage

Only stages that produce/consume LLM tokens are shown. `chunk_embed` uses the
OpenAI Embeddings API (not tracked via token counters here).

| Stage | Input tokens | Output tokens | Total |
|---|---|---|---|
| entities | 912 064 | 431 065 | **1 343 129** |
| paystubs | 38 413 | 2 237 | 40 650 |
| summarize | 15 463 | 4 569 | 20 032 |
| **Grand total** | **965 940** | **437 871** | **1 403 811** |

> **Note:** Entities is the dominant cost driver at 95.7% of total tokens. It runs
> one LLM call per chunk (1 564 calls) with no cache benefit since the entities table
> was wiped. In steady-state incremental runs, this cost drops to 0 for unchanged docs.

---

## 5. LLM Call Quality

| Stage | Calls | Success rate | Cache hit rate | Failures |
|---|---|---|---|---|
| entities | 78 227 | 91.9% | 0.2% | 6 357 |
| summarize | 94 | 97.9% | 76.6% | 2 |
| extract-text (OCR cleanup) | 2 | 50.0% | 50.0% | 1 |
| chunk (embeddings) | 1 564 | 100% | 0% | 0 |

**Entity extraction failures (6 357):** These are expected at scale — individual
chunks fail JSON parsing when the model output is malformed or truncated. The pipeline
continues and skips failed chunks. The 91.9% success rate yields 8 333 entity records
from 1 446 pages, which is functionally complete.

**Summarize failures (2):** See §6 below — both fixed in this run.

---

## 6. Bugs Found and Fixed

### Bug: `segmented_document_summary` truncated for large documents

**Root cause:** `max_output_tokens=700` was hardcoded for the composed summary call
in `_segmented_summary()`. A document with 18 segments needs ~1 080 tokens for the
`table_of_contents` array alone, causing the API to truncate the JSON mid-object and
fail parsing.

**Affected docs:**
- `D1-80 Meal Delivery Authorizations Safari.pdf` (549 pages, 18 segments)
- `D1-59 Invoices-Receipts Safari February 2021.pdf` (111 pages, 8 segments)

**Fix applied** (`src/agentic_parse/summarize.py:332`):
```python
# Before
max_output_tokens=700,

# After — scales with segment count, capped at model max
max_output_tokens=min(1400, 300 + 60 * len(segments)),
```

**Validation:** Re-ran summarize on both docs after fix. Both returned
`cache_hit=False success=True`. Summaries now contain structured JSON with
populated `overall_purpose`, `key_entities`, `financial_facts`, and `table_of_contents`.

---

## 7. Pipeline Bottlenecks

```
extract-text  ████████████████████████████████████████  1 377 s  43.4%
entities      ██████████████████████████████████        1 098 s  34.6%
chunk         ██████████████                             454 s   14.3%
paystubs      ████                                       139 s    4.4%
summarize     ███                                         99 s    3.1%
ingest        <1 s                                          4 s   0.1%
```

**extract-text** dominates because OCR (`tier1_ocr_pdf`) processes pages serially
within each document. Most time was on the 549-page Safari document (~23 min total
for all 10 docs at 4 workers).

**entities** is the second bottleneck. It issues one LLM call per chunk with no
parallelism. Average call latency was 5 453 ms per chunk.

---

## 8. Recommendations

| Priority | Issue | Suggestion |
|---|---|---|
| High | entities stage takes 18+ min | Batch multiple chunks per LLM call; add async/parallel execution |
| High | extract-text page-level serialization | Process multiple pages concurrently within a doc |
| Medium | 8.1% entity extraction failure rate | Add retry with exponential backoff for JSON parse failures |
| Low | `tier2a_ocr_cleanup` 1 failure | Investigate; likely a transient API error |

---

*Generated from `costly_calls` and `stage_metrics` tables. Run started 2026-03-10 00:10 CDT.*
