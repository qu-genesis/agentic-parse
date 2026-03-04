**Acceptance Criteria\
Approach B Workflow: Extract First with Selective LLM**

*Version 1.0 - March 4, 2026*

# 1. Purpose and Scope

This document defines the mandatory acceptance criteria for a scalable,
token efficient, multi modal investigative document workflow. The system
must extract text and small evidence snippets first and use LLM calls
primarily for structured extraction and reasoning. Vision capable LLM
OCR is permitted only as a controlled fallback path.

# 2. Guiding Principles

-   Convert, OCR, or transcribe each source once and cache results.

-   Prefer deterministic parsing over LLM based transcription.

-   Use retrieval first prompts with strict token caps.

-   Require evidence pointers for every extracted fact.

-   All stages must be idempotent and resumable.

# 3. Acceptance Criteria

## AC-01: Stable hashing and deduplicated ingestion

-   Each file receives a sha256 hash and stable document_id.

-   Media type is detected and recorded.

-   Duplicate hashes are not reprocessed.

**Pass condition:** Re running ingestion produces identical document IDs
and no duplicate work.

## AC-02: Catalogue without LLM or OCR dependency

-   Catalogue rows include document_id, hash, path, size, and
    media_type.

-   PDF page_count and audio or video duration are recorded when
    available.

-   summary_status initializes to pending_transcription.

**Pass condition:** Catalogue generation completes without triggering
OCR, ASR, or LLM calls.

## AC-03: Text layer PDFs bypass OCR and vision models

-   Text layer pages are parsed deterministically.

-   No OCR or vision LLM calls are made for those pages.

**Pass condition:** Logs confirm zero OCR or vision usage for text layer
pages.

## AC-04: Scanned pages use traditional OCR by default

-   Scanned pages are processed with deterministic OCR.

-   Per page OCR confidence or proxy metric is recorded.

-   Transcripts are stored in canonical transcript storage.

**Pass condition:** Re runs skip unchanged pages and reuse cached
transcripts.

## AC-05: Vision fallback is controlled and auditable

-   Fallback triggers include low OCR confidence or failed validation.

-   Each fallback records trigger reason and region used.

-   Fallback outputs are cached by page hash and model version.

**Pass condition:** Vision calls are a minority of pages and each has a
recorded trigger.

## AC-06: Stable chunking with provenance

-   Chunks have stable chunk_id values.

-   Each chunk records document_id and page number or timestamp range.

-   Chunk text is stored as an artifact.

**Pass condition:** Any chunk can be traced back to its exact source
location.

## AC-07: Embeddings are incremental and versioned

-   Each chunk receives an embedding stored in a vector index.

-   Embeddings are recomputed only if chunk text changes.

**Pass condition:** Re running embeddings on unchanged data performs
near zero new work.

## AC-08: Retrieval first LLM extraction with hard caps

-   Prompts include only top k relevant chunks.

-   Max token and max chunk limits are enforced in code.

-   Whole document submission is prevented by design.

**Pass condition:** The system rejects extraction requests that exceed
configured limits.

## AC-09: Structured payment record extraction with validation

-   Documents may be payment sheets, receipts, invoices, and optional
    pay stubs.

-   No fixed field set is mandatory across all payment records.

-   Currency parsing is deterministic where monetary values are present.

-   Numeric consistency checks are enforced only for fields that are
    present (for example, net versus gross when both are available).

**Pass condition:** Records with ambiguous or inconsistent monetary data
are flagged for review and are not silently accepted.

## AC-10: Entity registry with evidence backed relationships

-   Entities have stable entity_id values and alias support.

-   Relationships include subject, object, type, confidence, and
    evidence pointer.

-   Evidence pointer references page or timestamp location.

**Pass condition:** No relationship exists without at least one evidence
pointer.

## AC-11: Audio and video are processed via ASR first

-   Audio files are transcribed once with timestamps.

-   Video is processed as audio plus sparse keyframes.

-   Extraction references timestamp evidence.

**Pass condition:** Audio derived facts can be traced to timestamp
ranges.

## AC-12: Text messages support exports and screenshots

-   Structured exports are parsed deterministically.

-   Screenshots are OCR processed before any vision fallback.

-   Thread summaries operate on transcripts rather than raw images.

**Pass condition:** Text processing avoids bulk vision calls on entire
screenshot sets.

## AC-13: Idempotent, resumable, and observable pipeline

-   All stages are safe to retry.

-   Artifacts are written atomically.

-   Metrics capture processed, skipped, failed, and token usage.

**Pass condition:** Killing and restarting the pipeline resumes without
duplicating completed work.

## AC-14: End to end and incremental tests exist

-   A gold corpus covers PDFs, payment records (including optional pay
    stubs), audio, screenshots, and video.

-   Incremental test proves only new documents are processed on re run.

**Pass condition:** Automated tests validate schema compliance and
incremental behavior.
