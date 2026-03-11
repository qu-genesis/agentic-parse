# agentic-parse

Scalable multimodal document ETL where deterministic parsing does first-pass extraction and `gpt-4o` is used for constrained downstream reasoning.

## Current implementation

- Immutable ingest with `sha256` → stable `document_id` and dedupe.
- Catalogue-first pass without OCR/ASR/LLM dependency. Catalogue JSONL is refreshed from DB state after each summarize run so statuses stay current.
- Tiered extraction with quality-gated escalation:
  - **Tier-0 (multi-backend)**: PyMuPDF → pdfplumber → pypdf per page; best candidate selected by text quality score.
  - **Text quality scoring**: 6-signal composite (alpha ratio, printable ratio, unique-char ratio, short-line ratio, whitespace dominance, word density). Pages below the quality threshold escalate even when text is present — catches garbled extractions, vertical text, and near-empty pages.
  - **Tier-1 OCR**: tesseract with 4-rotation search (0°/90°/180°/270°) + grayscale/sharpen preprocessing; best-confidence orientation used for final extraction.
  - **Tier-2A (LLM text cleanup)**: text-in/text-out cleanup for noisy OCR on text-dominant pages.
  - **Tier-2B (Vision OCR)**: rendered page image sent to a vision-capable LLM for faithful transcription; used for visually complex page types (tables, forms, handwriting, invoices). Layout-specific prompts per page type.
  - Audio/video: ASR-first path with timestamp provenance (placeholder fallback if ASR unavailable).
  - Chat exports: deterministic parser.
- **Page-type classification**: deterministic heuristics classify every page as one of `invoice`, `table`, `form`, `handwritten`, `comment_box`, `text`, or `blank`. Stored in the `pages` table; drives Tier-2A/2B routing and segment labels in summaries.
- **Long PDF segmentation**: documents ≥ 20 pages are split into thematic segments via adjacent-page Jaccard similarity change-point detection. Each segment is summarized independently, then a composed document summary includes a section index with page ranges and dominant page types.
- Stable chunking + embedding lifecycle (`chunk_text_hash`, embedding model/version) + vector index table. Documents with all-empty pages (unreadable scans) are marked `status_embed = done` without blocking downstream stages.
- Retrieval-first query extraction with hard limits (`top_k`, `max_chunks`, `max_tokens`).
- Payment-record extraction (receipts/invoices/payment sheets; pay-stub fields optional) via retrieval-first `gpt-4o` extraction with validator checks.
- Incremental entity/relationship extraction with page/timestamp evidence pointers.
- Idempotent pipeline behavior, atomic artifact writes, and stage/fallback metrics.

## Architecture diagram

```mermaid
flowchart TD
    A["Raw files<br/>PDF, IMG, TXT, AUD, VID, chat export"] --> B["Ingest and catalogue"]
    B --> C[("State DB (PostgreSQL + pgvector)<br/>documents, pages, chunks,<br/>vector_index, relationships,<br/>paystubs, fallback_events, stage_metrics")]

    B --> D{"doc_family?"}
    D -->|pdf| P0["Tier-0 multi-backend text extraction<br/>(PyMuPDF → pdfplumber → pypdf)<br/>scored by text quality"]
    D -->|image or screenshot| I0["Tier-1 OCR image"]
    D -->|text| T0["Tier-0 plain text parse"]
    D -->|audio or video| AV0["Tier-0 ASR"]
    D -->|chat_export| CH0["Tier-0 chat export parser"]

    P0 --> PQ{"Text quality score<br/>≥ threshold?"}
    PQ -->|yes| PT["Accept Tier-0 text"]
    PQ -->|no| P2["Tier-1 OCR with orientation search<br/>0°/90°/180°/270° + preprocessing"]

    P2 --> P3{"OCR confidence < 0.35?"}
    I0 --> I1{"OCR confidence < 0.30?"}

    P3 -->|yes| PC["Classify page type"]
    I1 -->|yes| PC

    PC -->|table, form,<br/>handwritten, invoice| L2["Tier-2B Vision OCR<br/>(image → vision LLM)<br/>layout-specific prompts"]
    PC -->|text, unknown| L1["Tier-2A LLM text cleanup<br/>(text-in / text-out)"]

    P3 -->|no| X0["Use Tier-1 OCR text"]
    I1 -->|no| X0

    PT --> CL["Classify page type<br/>(invoice/table/form/<br/>handwritten/comment_box/text)"]
    X0 --> CL
    L1 --> CL
    L2 --> CL

    P0 --> E["Transcript artifacts per page"]
    T0 --> E
    AV0 --> E
    CH0 --> E
    CL --> E

    E --> C
    E --> F["Chunk and embed<br/>stable chunk_id + chunk_text_hash"]
    F --> C
    F --> V["Vector index upsert (pgvector)"]
    V --> C

    E --> SZ{"Page count<br/>≥ 20?"}
    SZ -->|yes| SS["Segmented summary<br/>Jaccard change-point detection<br/>per-segment LLM summary<br/>+ composed document summary"]
    SZ -->|no| SF["Flat summary<br/>retrieval-first (gpt-4o)"]

    F --> R["Retrieval top-k from vector_index<br/>max_chunks + max_tokens enforced"]
    R --> EN["Entity and relationship extraction (gpt-4o)"]
    E --> PAY["Payment-record extraction from retrieved chunks (gpt-4o)"]

    EN --> O1["relationships.jsonl + entity cards"]
    PAY --> O2["paystubs.jsonl (payment records)"]
    SS --> O3["document summaries (segmented)"]
    SF --> O3

    C --> M["Observability outputs<br/>stage_metrics + fallback_events"]

    classDef det fill:#E8F5E9,stroke:#1B5E20,color:#1B5E20,stroke-width:1px;
    classDef llm fill:#FFF3E0,stroke:#E65100,color:#BF360C,stroke-width:1px;
    classDef state fill:#E3F2FD,stroke:#0D47A1,color:#0D47A1,stroke-width:1px;
    classDef decision fill:#F5F5F5,stroke:#424242,color:#212121,stroke-width:1px;

    class A,B,D,P0,PT,P2,I0,T0,AV0,CH0,X0,CL,PC,E,F,V,R,O1,O2,O3,M det;
    class L1,L2,EN,SS,SF,PAY llm;
    class C state;
    class PQ,P3,I1,SZ decision;
```

## Queue orchestration diagram

```mermaid
flowchart TD
    Q0["Queue 0: ingest_queue"] --> W0["Worker: ingest and catalogue"]
    W0 --> DB[("State DB")]
    W0 --> Q1["Queue 1: extract_queue"]

    Q1 --> W1["Worker: extract router"]
    W1 --> D0{"doc_family?"}

    D0 -->|pdf| H1["Handler: Tier-0 multi-backend<br/>(PyMuPDF / pdfplumber / pypdf)<br/>quality scored"]
    D0 -->|image or screenshot| H3["Handler: tier1 OCR image"]
    D0 -->|text| H4["Handler: tier0 text parse"]
    D0 -->|audio or video| H5["Handler: tier0 ASR"]
    D0 -->|chat export| H6["Handler: tier0 chat parser"]

    H1 --> D0B{"Quality score<br/>≥ threshold?"}
    D0B -->|no| H2["Handler: Tier-1 OCR<br/>orientation search + preprocessing"]
    D0B -->|yes| CL["Classify page type"]

    H2 --> D1{"confidence < 0.35?"}
    H3 --> D2{"confidence < 0.30?"}

    D1 -->|yes| PT["Classify page type"]
    D2 -->|yes| PT
    PT -->|visual page type| Q2B["Queue 2B: vision_ocr_queue"]
    PT -->|text page type| Q2A["Queue 2A: llm_fallback_queue (rate-limited)"]

    D1 -->|no| CL
    D2 -->|no| CL

    Q2A --> WLA["Worker: gpt-4o text cleanup"]
    Q2B --> WLB["Worker: vision LLM OCR"]
    WLA --> CL
    WLB --> CL

    CL --> T0["Transcript write + page_type stored"]

    H4 --> T0
    H5 --> T0
    H6 --> T0

    T0 --> DB
    T0 --> Q3["Queue 3: chunk_embed_queue"]

    Q3 --> W3["Worker: chunk and embed"]
    W3 --> DB
    W3 --> VI["Upsert vector_index"]
    VI --> DB

    W3 --> Q4["Queue 4: summarize_queue"]
    W3 --> Q5["Queue 5: entities_queue"]
    T0 --> Q6["Queue 6: payment_queue"]

    Q4 --> WS{"Page count ≥ 20?"}
    WS -->|yes| WSS["Worker: segmented summary<br/>Jaccard segments + per-segment gpt-4o"]
    WS -->|no| WSF["Worker: flat summary<br/>retrieval + gpt-4o"]
    WSS --> DB
    WSF --> DB

    Q5 --> WE["Worker: retrieval + entities gpt-4o"]
    WE --> DB
    Q6 --> WP["Worker: payment records (retrieval + gpt-4o)"]
    WP --> DB

    DB --> OBS["stage_metrics and fallback_events"]

    classDef det fill:#E8F5E9,stroke:#1B5E20,color:#1B5E20,stroke-width:1px;
    classDef llm fill:#FFF3E0,stroke:#E65100,color:#BF360C,stroke-width:1px;
    classDef state fill:#E3F2FD,stroke:#0D47A1,color:#0D47A1,stroke-width:1px;
    classDef decision fill:#F5F5F5,stroke:#424242,color:#212121,stroke-width:1px;
    classDef queue fill:#EDE7F6,stroke:#4527A0,color:#311B92,stroke-width:1px;

    class W0,W1,H1,H2,H3,H4,H5,H6,CL,T0,W3,VI,OBS det;
    class WLA,WLB,WE,WSS,WSF,WP llm;
    class DB state;
    class D0,D0B,D1,D2,WS decision;
    class Q0,Q1,Q2A,Q2B,Q3,Q4,Q5,Q6 queue;
```

## Repository layout

- `src/agentic_parse/cli.py`: CLI entrypoint and stage orchestration.
- `src/agentic_parse/db.py`: schema + migration-safe initialization (including `page_type` column).
- `src/agentic_parse/ingest.py`: hashing, dedupe, catalogue rows.
- `src/agentic_parse/extract_text.py`: tiered OCR/ASR/transcript pipeline — quality scoring, multi-backend Tier-0, orientation-aware Tier-1, Tier-2A/2B routing, page-type classification.
- `src/agentic_parse/chunk_embed.py`: chunking, embedding lifecycle, retrieval helper.
- `src/agentic_parse/entities.py`: entity/relationship extraction + retrieval-first query mode.
- `src/agentic_parse/paystub.py`: embedding-retrieved LLM payment-record extraction + validation.
- `src/agentic_parse/summarize.py`: flat and segmented LLM document summaries; catalogue JSONL refresh.
- `src/agentic_parse/llm.py`: OpenAI client wrapper + caching.
- `src/agentic_parse/telemetry.py`: metrics and fallback audit logging.

## Data model highlights

- `document_id = doc_<sha256_prefix>`
- `page_id = <document_id>_p<page_number>`
- `chunk_id = <page_id>_c<chunk_index>`

Core tables:

- `documents`: media metadata, durations, lifecycle statuses.
- `pages`: source tier/pointer, OCR confidence, timestamps, fallback metadata, **`page_type`** label.
- `chunks`: text span metadata + embedding hash/model/version.
- `vector_index`: pgvector records by chunk (HNSW index).
- `relationships`: evidence-backed edges with page/timestamp pointers.
- `paystubs`: generalized payment records + validation status.
- `fallback_events`: auditable fallback triggers (including `bad_text_layer`, `vertical_or_fragmented_text`, `empty_or_near_empty`) and model/version references.
- `stage_metrics`: processed/skipped/failed/token usage per stage run.

## Text extraction tiers

| Tier | Method | Trigger |
|------|--------|---------|
| 0A | PyMuPDF (`fitz`) | Always tried first |
| 0B | pdfplumber | Tried alongside 0A; best quality wins |
| 0C | pypdf | Tried alongside 0A/0B; best quality wins |
| 1 | tesseract OCR (4 rotations + preprocessing) | Tier-0 quality score below threshold |
| 2A | LLM text cleanup (gpt-4o) | Tier-1 confidence < 0.35, text/unknown page type |
| 2B | Vision OCR (gpt-4o vision) | Tier-1 confidence < 0.35, visual page type (table/form/handwritten/invoice) |

## Page types

Pages are classified after extraction and stored in the `pages` table:

| Type | Detection signals |
|------|------------------|
| `invoice` | ≥2 of: invoice, receipt, bill to, ship to, subtotal, total due, tax, amount due |
| `table` | >30% of lines with ≥3 numeric tokens, or >40% with ≥2 whitespace-aligned columns |
| `form` | ≥2 checkbox characters or ≥3 form field labels (name/date/signature/address/etc.) |
| `handwritten` | Tier-1 confidence < 0.35 + >50% short tokens |
| `comment_box` | Keywords: comment, response, author, reply, reviewed by |
| `text` | Default for clean text content |
| `blank` | Empty content |

## Quick start

```bash
export OPENAI_API_KEY="<your-key>"
export OPENAI_MODEL="gpt-4o"          # default is gpt-4o-mini

python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw all --workers 4
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw status
```

## Stage commands

```bash
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw ingest
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw extract-text --workers 8
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw chunk
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw summarize
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw entities
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw paystubs
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw extract-query --query "Who appears with Acme?" --top-k 8 --max-chunks 20 --max-tokens 6000
python -m agentic_parse.cli --workspace ./workspace --raw-root ./raw status
```

## Outputs

- `workspace/outputs/document_catalogue.jsonl` — refreshed after each summarize run
- `workspace/outputs/document_summary_catalogue.json` — grouped catalogue generated from short document summaries
- `workspace/outputs/relationships.jsonl`
- `workspace/outputs/paystubs.jsonl`
- `workspace/outputs/fallback_events.jsonl`
- `workspace/outputs/stage_metrics.jsonl`
- `workspace/outputs/vector_index.jsonl`
- `workspace/outputs/entities/ent_*.json`
- `workspace/derived/summaries/<doc_id>/document.summary.txt` — flat or segmented

## Dependencies

Required:

- `psycopg2-binary` + PostgreSQL with `pgvector` extension
- `openai`: gpt-4o reasoning, vision OCR (Tier-2B), and ASR

Optional (gracefully degraded if absent):

- `pymupdf` (`fitz`): best-quality Tier-0 PDF text extraction
- `pdfplumber`: table-aware Tier-0 extraction
- `pypdf`: fast Tier-0 fallback (always included)
- `pytesseract` + `Pillow`: Tier-1 OCR
- `pypdfium2`: PDF page rendering for Tier-1 OCR
- `ffprobe` (system): media duration probing

## Tests

```bash
pytest
```
