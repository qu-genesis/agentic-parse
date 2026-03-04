# AC Fix Status

Date: 2026-03-04

- AC-01: Implemented (`sha256`, stable `document_id`, dedupe ingest)
- AC-02: Implemented (catalog fields + optional PDF page count + audio/video duration + `summary_status=pending_transcription`)
- AC-03: Implemented (text-layer PDF deterministic path + stage tier metrics)
- AC-04: Implemented (deterministic OCR for scanned images/PDF pages before fallback)
- AC-05: Implemented (fallback trigger reason/region + fallback audit records + cache keyed by page hash/model)
- AC-06: Implemented (stable chunks + source provenance + chunk artifacts)
- AC-07: Implemented (embedding model/version/text-hash lifecycle + vector index table/output)
- AC-08: Implemented (retrieval-first `extract-query` with hard caps and enforced rejection)
- AC-09: Implemented (broadened payment-record extraction for receipts/invoices/payment sheets with optional pay-stub fields; deterministic currency parsing; conditional consistency validation)
- AC-10: Implemented (stable entities + evidence-backed relationships with page/timestamp pointers)
- AC-11: Implemented (audio/video ASR-first flow with timestamp provenance; placeholder fallback when ASR unavailable)
- AC-12: Implemented (deterministic chat export parser; screenshot OCR-first path)
- AC-13: Implemented (idempotent stages, atomic artifact writes, metrics table/jsonl)
- AC-14: Implemented (automated tests added under `tests/`)
