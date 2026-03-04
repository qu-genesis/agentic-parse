# agentic-parse

Scalable multimodal document ETL where deterministic parsing does first-pass extraction and `gpt-4o` is used for constrained downstream reasoning.

## Current implementation

- Immutable ingest with `sha256` -> stable `document_id` and dedupe.
- Catalogue-first pass without OCR/ASR/LLM dependency.
- Tiered extraction:
  - Text-layer PDFs: deterministic parse (`pypdf`).
  - Scanned images/PDF pages: deterministic OCR first (`pytesseract`, `pypdfium2`).
  - Controlled LLM fallback only when needed, with audit trail.
  - Audio/video: ASR-first path with timestamp provenance (placeholder fallback if ASR unavailable).
  - Chat exports: deterministic parser.
- Stable chunking + embedding lifecycle (`chunk_text_hash`, embedding model/version) + vector index table.
- Retrieval-first query extraction with hard limits (`top_k`, `max_chunks`, `max_tokens`).
- Payment-record extraction (receipts/invoices/payment sheets; pay-stub fields optional) with deterministic currency parsing and conditional consistency checks.
- Incremental entity/relationship extraction with page/timestamp evidence pointers.
- Idempotent pipeline behavior, atomic artifact writes, and stage/fallback metrics.

## Architecture diagram

```mermaid
flowchart TD
    A["Raw files<br/>PDF, IMG, TXT, AUD, VID, chat export"] --> B["Ingest and catalogue"]
    B --> C[("State DB (SQLite)<br/>documents, pages, chunks,<br/>vector_index, relationships,<br/>paystubs, fallback_events, stage_metrics")]

    B --> D{"doc_family?"}
    D -->|pdf| P0{"PDF page has text layer?"}
    D -->|image or screenshot| I0["Tier-1 OCR image"]
    D -->|text| T0["Tier-0 plain text parse"]
    D -->|audio or video| AV0["Tier-0 ASR"]
    D -->|chat_export| CH0["Tier-0 chat export parser"]

    P0 -->|yes| P1["Tier-0 PDF text extraction"]
    P0 -->|no| P2["Tier-1 OCR rendered PDF page"]

    P2 --> P3{"OCR confidence < 0.35?"}
    I0 --> I1{"OCR confidence < 0.30?"}
    P3 -->|yes| L1["Tier-2 LLM OCR cleanup (gpt-4o)<br/>cached by page_hash + model"]
    I1 -->|yes| L1
    P3 -->|no| X0["Use Tier-1 OCR text"]
    I1 -->|no| X0

    P1 --> E["Transcript artifacts per page or segment"]
    T0 --> E
    AV0 --> E
    CH0 --> E
    X0 --> E
    L1 --> E

    E --> C
    E --> F["Chunk and embed<br/>stable chunk_id + chunk_text_hash"]
    F --> C
    F --> V["Vector index upsert (SQLite table + JSONL)"]
    V --> C

    E --> S["Document summary"]
    F --> R["Retrieval top-k from vector_index<br/>max_chunks + max_tokens enforced"]
    R --> EN["Entity and relationship extraction (gpt-4o)"]
    E --> PAY["Payment-record extraction"]

    EN --> O1["relationships.jsonl + entity cards"]
    PAY --> O2["paystubs.jsonl (payment records)"]
    S --> O3["document summaries"]

    C --> M["Observability outputs<br/>stage_metrics + fallback_events"]

    classDef det fill:#E8F5E9,stroke:#1B5E20,color:#1B5E20,stroke-width:1px;
    classDef llm fill:#FFF3E0,stroke:#E65100,color:#BF360C,stroke-width:1px;
    classDef state fill:#E3F2FD,stroke:#0D47A1,color:#0D47A1,stroke-width:1px;
    classDef decision fill:#F5F5F5,stroke:#424242,color:#212121,stroke-width:1px;

    class A,B,D,P0,P1,P2,I0,T0,AV0,CH0,X0,E,F,V,S,R,PAY,O1,O2,O3,M det;
    class L1,EN llm;
    class C state;
    class P3,I1 decision;
```

## Queue orchestration diagram

```mermaid
flowchart TD
    Q0["Queue 0: ingest_queue"] --> W0["Worker: ingest and catalogue"]
    W0 --> DB[("State DB")]
    W0 --> Q1["Queue 1: extract_queue"]

    Q1 --> W1["Worker: extract router"]
    W1 --> D0{"doc_family?"}

    D0 -->|pdf text-layer| H1["Handler: tier0 PDF text"]
    D0 -->|pdf scanned| H2["Handler: tier1 OCR PDF page"]
    D0 -->|image or screenshot| H3["Handler: tier1 OCR image"]
    D0 -->|text| H4["Handler: tier0 text parse"]
    D0 -->|audio or video| H5["Handler: tier0 ASR"]
    D0 -->|chat export| H6["Handler: tier0 chat parser"]

    H2 --> D1{"confidence < 0.35?"}
    H3 --> D2{"confidence < 0.30?"}
    D1 -->|yes| Q2["Queue 2: llm_fallback_queue (rate-limited)"]
    D2 -->|yes| Q2
    D1 -->|no| T0["Transcript write"]
    D2 -->|no| T0

    Q2 --> WL["Worker: gpt-4o OCR cleanup"]
    WL --> T0

    H1 --> T0
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

    Q4 --> WS["Worker: summarize"]
    WS --> DB
    Q5 --> WE["Worker: retrieval + entities gpt-4o"]
    WE --> DB
    Q6 --> WP["Worker: payment record parser"]
    WP --> DB

    DB --> OBS["stage_metrics and fallback_events"]

    classDef det fill:#E8F5E9,stroke:#1B5E20,color:#1B5E20,stroke-width:1px;
    classDef llm fill:#FFF3E0,stroke:#E65100,color:#BF360C,stroke-width:1px;
    classDef state fill:#E3F2FD,stroke:#0D47A1,color:#0D47A1,stroke-width:1px;
    classDef decision fill:#F5F5F5,stroke:#424242,color:#212121,stroke-width:1px;
    classDef queue fill:#EDE7F6,stroke:#4527A0,color:#311B92,stroke-width:1px;

    class W0,W1,H1,H2,H3,H4,H5,H6,T0,W3,VI,WS,WP,OBS det;
    class WL,WE llm;
    class DB state;
    class D0,D1,D2 decision;
    class Q0,Q1,Q2,Q3,Q4,Q5,Q6 queue;
```

## Repository layout

- `src/agentic_parse/cli.py`: CLI entrypoint and stage orchestration.
- `src/agentic_parse/db.py`: schema + migration-safe initialization.
- `src/agentic_parse/ingest.py`: hashing, dedupe, catalogue rows.
- `src/agentic_parse/extract_text.py`: OCR/ASR/transcript pipeline.
- `src/agentic_parse/chunk_embed.py`: chunking, embedding lifecycle, retrieval helper.
- `src/agentic_parse/entities.py`: entity/relationship extraction + retrieval-first query mode.
- `src/agentic_parse/paystub.py`: payment-record parsing/validation.
- `src/agentic_parse/summarize.py`: transcript-first summaries.
- `src/agentic_parse/llm.py`: OpenAI client wrapper + caching.
- `src/agentic_parse/telemetry.py`: metrics and fallback audit logging.

## Data model highlights

- `document_id = doc_<sha256_prefix>`
- `page_id = <document_id>_p<page_number>`
- `chunk_id = <page_id>_c<chunk_index>`

Core tables:

- `documents`: media metadata, durations, lifecycle statuses.
- `pages`: source tier/pointer, OCR confidence, timestamps, fallback metadata.
- `chunks`: text span metadata + embedding hash/model/version.
- `vector_index`: vector records by chunk.
- `relationships`: evidence-backed edges with page/timestamp pointers.
- `paystubs`: generalized payment records + validation status.
- `fallback_events`: auditable fallback triggers and model/version references.
- `stage_metrics`: processed/skipped/failed/token usage.

## Quick start

```bash
export OPENAI_API_KEY="<your-key>"   # optional but recommended
export OPENAI_MODEL="gpt-4o"          # default is gpt-4o

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

- `workspace/outputs/document_catalogue.jsonl`
- `workspace/outputs/relationships.jsonl`
- `workspace/outputs/paystubs.jsonl`
- `workspace/outputs/fallback_events.jsonl`
- `workspace/outputs/stage_metrics.jsonl`
- `workspace/outputs/vector_index.jsonl`
- `workspace/outputs/entities/ent_*.json`

## Optional dependencies

- `openai`: `gpt-4o` reasoning + ASR.
- `pypdf`: text-layer PDF extraction.
- `pytesseract` + `Pillow`: OCR.
- `pypdfium2`: PDF page rendering for OCR.
- `ffprobe` (system): media duration probing.

## Tests

```bash
pytest
```
