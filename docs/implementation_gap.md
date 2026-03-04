# Implementation Gap Assessment

Source: `Acceptance_Criteria_Approach_B_Workflow.md`
Scope: Current codebase under `src/agentic_parse`
Date: 2026-03-04

## Acceptance Status Matrix

| ID | Status | Notes |
|---|---|---|
| AC-01 | Pass | Stable `sha256` hashing and deterministic `document_id`; duplicates skipped in ingest. |
| AC-02 | Partial | Catalogue includes required core fields and PDF page count, but no audio/video duration and no `summary_status=pending_transcription` field. |
| AC-03 | Partial | Text-layer PDFs are parsed deterministically; however, there is no explicit observability/log proof for zero OCR/vision usage. |
| AC-04 | Fail | Scanned PDFs are not OCRed by default; image OCR exists, but PDF pages with no text currently route to fallback behavior rather than deterministic OCR pipeline. |
| AC-05 | Fail | Vision fallback exists and is cached, but trigger reason/region are not recorded and cache key is not explicitly `(page_hash + model_version)`. |
| AC-06 | Pass | Stable `chunk_id`; provenance includes `document_id`, `page_number`, `char_start/end`; chunk artifact persisted. |
| AC-07 | Fail | Embeddings are placeholders in SQLite (not vector index), lack embedding versioning, and do not invalidate/recompute when chunk text changes. |
| AC-08 | Fail | No retrieval-first top-k selection with hard caps for downstream extraction; extraction runs across all pending chunks. |
| AC-09 | Fail | No pay-stub schema extraction, deterministic currency parsing, or numeric consistency validation logic. |
| AC-10 | Pass | Stable entity IDs with aliases; relationships include subject/object/type/confidence and evidence pointer (document/page excerpt). |
| AC-11 | Fail | No ASR pipeline for audio; no video->audio/keyframe flow; no timestamp-grounded extraction. |
| AC-12 | Fail | No dedicated text-message export parser, screenshot-thread workflow, or transcript-first thread summarization path. |
| AC-13 | Partial | Stages are mostly idempotent/resumable, but writes are not atomic and metrics do not include processed/skipped/failed/token usage. |
| AC-14 | Fail | No automated gold-corpus tests or incremental re-run validation suite. |

## Detailed Gaps and Required Changes

### 1. Catalogue completeness gaps (AC-02)
- Missing fields:
  - `audio_duration_seconds` / `video_duration_seconds`
  - `summary_status` initialized to `pending_transcription`
- Required changes:
  - Extend `documents` schema with duration + summary status columns.
  - Add media probing in ingest (e.g., ffprobe or equivalent optional dependency).
  - Append new fields into `document_catalogue.jsonl`.

### 2. Scanned PDF OCR path missing (AC-04)
- Current behavior:
  - PDF extraction uses text-layer parsing only.
  - Empty PDF page text can route to fallback path, not deterministic OCR for rendered PDF pages.
- Required changes:
  - Add PDF page rendering + deterministic OCR tier for scanned pages.
  - Record per-page OCR confidence and mark source tier as deterministic OCR before fallback.

### 3. Fallback auditability insufficient (AC-05)
- Missing records:
  - trigger reason (`low_confidence`, `validation_failed`, etc.)
  - region/crop metadata
  - explicit cache identity on page hash + model version
- Required changes:
  - Add fallback audit table or JSONL log for each fallback event.
  - Persist trigger reason, region bbox, model version, and page hash.

### 4. Embedding lifecycle not production-compliant (AC-07)
- Current behavior:
  - Embeddings are deterministic placeholders in SQLite text field.
  - No vector DB index integration.
  - No embedding version column/hash invalidation strategy.
- Required changes:
  - Add `embedding_model`, `embedding_version`, and `chunk_text_hash` metadata.
  - Recompute embeddings when chunk hash or model version changes.
  - Push vectors to vector index (pgvector/Qdrant/etc.).

### 5. Retrieval-first hard-cap enforcement missing (AC-08)
- Current behavior:
  - Entity extraction processes all pending chunks directly.
  - No top-k retrieval gate and no max token/chunk rejection path.
- Required changes:
  - Introduce query-time retrieval API selecting top-k chunks.
  - Enforce hard `max_chunks` and token budget in code.
  - Reject oversized extraction requests deterministically.

### 6. Pay-stub extraction/validation not implemented (AC-09)
- Missing:
  - required pay-stub schema extraction
  - deterministic currency parsing
  - gross/net consistency checks and fallback-to-review path
- Required changes:
  - Add dedicated pay-stub extractor module with strict validation and review status output.

### 7. Audio/video pipeline missing (AC-11)
- Missing:
  - ASR-first transcription for audio with timestamps
  - video to audio + sparse keyframe flow
  - timestamp-grounded evidence in relationships/facts
- Required changes:
  - Add media transcription stage and timestamp-aware chunking/provenance fields.

### 8. Text-message workflow not implemented (AC-12)
- Missing:
  - deterministic parser for structured chat exports
  - screenshot-set workflow: OCR first, vision fallback only when needed
  - transcript-based thread summaries
- Required changes:
  - Add dedicated message ingestion modes and screenshot thread assembler.

### 9. Observability + atomic writes gaps (AC-13)
- Current behavior:
  - Basic stage counters exist, but no structured metrics for processed/skipped/failed/token usage.
  - Artifacts written directly (not atomic temp->rename pattern).
- Required changes:
  - Add metrics table and per-stage counters/tokens.
  - Implement atomic file writes for artifacts.
  - Emit structured logs for retries/skips/failures.

### 10. Test coverage absent (AC-14)
- Missing:
  - gold corpus tests for PDFs/pay stubs/audio/screenshots/video
  - incremental rerun tests
  - schema compliance assertions
- Required changes:
  - Add `tests/` suite with fixtures and CI job running end-to-end + incremental tests.

## Suggested Prioritization

1. AC-04, AC-05, AC-07, AC-08 (core scalability/cost correctness)
2. AC-02, AC-13 (operational quality and observability)
3. AC-11, AC-12, AC-09 (domain/media completeness)
4. AC-14 (regression prevention and release gate)
