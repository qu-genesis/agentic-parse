# Provider Readiness Report

Date: 2026-03-04
Scope: Second-pass runtime provider validation for OCR/PDF/ASR/LLM integrations.

## Environment Check

- Python providers:
  - `openai`: installed (`2.24.0`)
  - `pypdf`: installed (`6.7.5`)
  - `pytesseract`: installed (`0.3.13`)
  - `Pillow`: installed (`10.1.0`)
  - `pypdfium2`: installed (`5.5.0`)
- System binaries:
  - `tesseract`: installed (`5.2.0`)
  - `ffprobe`: not installed
- OpenAI runtime env:
  - `OPENAI_API_KEY`: not set
  - model default: `gpt-4o`

## Integration Exercise Performed

A temporary multimodal fixture set was generated and processed through:

`ingest -> extract-text -> chunk -> summarize -> entities -> paystubs`

Fixture included:
- text-layer PDF
- scanned PDF
- screenshot image
- chat export JSON
- payment-record text (pay-stub shaped sample)
- audio WAV
- video placeholder

Observed source tiers from pipeline state:
- `tier0_text_layer`: exercised
- `tier1_ocr_pdf`: exercised
- `tier1_ocr_image`: exercised
- `tier0_chat_export_parser`: exercised
- `tier0_plain_text`: exercised
- `tier0_asr_placeholder`: exercised (expected without OpenAI key)

Fallback events:
- `0` in this run (OCR confidence sufficient for sample)

Stage metrics:
- Metrics recorded for `ingest`, `extract_text`, `chunk_embed`, `summarize`, `entities`, `paystub_extract`

## Readiness Status

- PDF text extraction: PASS
- Scanned PDF OCR: PASS
- Screenshot OCR-first path: PASS
- Chat export deterministic parsing: PASS
- Payment-record deterministic extraction/validation: PASS
- Metrics/fallback audit plumbing: PASS
- OpenAI live calls (`gpt-4o`): BLOCKED (missing `OPENAI_API_KEY`)
- Media duration probing: PARTIAL (blocked by missing `ffprobe`)
- Audio/video ASR with real transcription: PARTIAL (placeholder path active without key)

## Actions To Fully Validate Live Providers

1. Set `OPENAI_API_KEY` and rerun pipeline to verify live LLM/ASR usage and token accounting.
2. Install ffmpeg/ffprobe and rerun ingest to populate audio/video durations.
3. Run with a known low-quality scan to force Tier-2 fallback and verify `fallback_events` logging in a live-call scenario.
