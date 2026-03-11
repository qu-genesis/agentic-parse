Strategy: incrementally improve PDF parsing for heterogeneous documents

Below is a staged plan that keeps your deterministic-first philosophy, but makes it actually competent for mixed archetypes and long stitched PDFs.

Step 1 — Add a Tier-0 “text quality score” and trigger OCR when it’s bad

Replace:

if not text: ... OCR ...

with:

if is_bad_text_layer(text, page_meta): ... OCR ...

A good is_bad_text_layer(...) can be cheap + robust. Start with signals like:

Text-layer quality heuristics (fast, deterministic):

Character count < N (e.g., < 30) → likely empty/near-empty.

Alpha ratio: alpha_chars / total_chars too low (lots of symbols).

Printable ratio: high control/unicode replacement chars.

Unique char ratio extremely low (e.g., “IIIIIIII…”, “———”).

Line structure: many 1–2 character lines (classic bad extraction / vertical text).

Whitespace dominance.

Language-like score: a tiny wordlist check (or regex for common word shapes) can outperform you’d think.

Then:

If Tier-0 score is “bad”, treat it like “no text layer” and run Tier-1 OCR.

Record a fallback event reason like bad_text_layer (not just low_ocr_confidence), so you can measure improvements over time. The plumbing is already there.

This single change usually fixes the “pypdf didn’t escalate” class of problems.

Step 2 — Stop relying on pypdf alone for Tier-0 text

Keep Tier-0 deterministic, but make it multi-backend:

Tier-0A: try pymupdf (fitz) text extraction
Tier-0B: fall back to pdfminer.six / pdfplumber for tricky layout
Tier-0C: keep pypdf as a fast attempt if you like, but don’t let it be the only gate

Then choose the best text among candidates using the same quality scoring from Step 1.

Reason: different PDFs fail differently, and text-layer extraction reliability varies a lot by library.

Step 3 — Make OCR (Tier-1) smarter about rotation, vertical text, and “landscape capture”

Right now pypdfium2 renders and you run tesseract once.
For vertical pages / rotated scans, add orientation search:

Render at 0°, 90°, 180°, 270°

Run a lightweight OCR pass (or tesseract’s OSD) to pick the best confidence

Then run full OCR once at best orientation

Also add basic image preprocessing for OCR:

grayscale + binarization

deskew (even a simple Hough-line-based skew estimate)

increase scale (you already use scale=2.0—good start)

This directly targets your “vertical pages” archetype.

Step 4 — Redefine Tier-2 into two different fallbacks

Right now Tier-2 is “LLM cleanup of OCR text.”
That helps for noisy OCR, but it won’t fix:

handwriting

complex tables

checkboxes/forms

comment boxes with metadata

low-contrast scans where tesseract misses content entirely

So split Tier-2 into:

Tier-2A: LLM cleanup (text-in/text-out)
Keep what you have (cheap-ish tokens, good win rate).

Tier-2B: Vision OCR (image-in/text-out)
When Tier-1 confidence is low or the page looks visually complex, send the rendered page image to a vision-capable model and ask for faithful transcription with layout hints (preserve table rows, checkbox states, comment-author blocks, etc.).

This is the place where it can make sense to “call an LLM to OCR the document itself” — but only for the pages that need it.

Decision rule (simple and effective):

Tier-0 text quality good → accept Tier-0

else Tier-1 OCR

if Tier-1 confidence < threshold (you’re using 0.35 for PDFs today) → Tier-2A cleanup and/or Tier-2B vision OCR depending on page type

Step 5 — Add page-type classifiers to handle your archetypes

To “represent the heterogeneous nature” in summaries, you need page labeling (even if imperfect):

Deterministic or lightweight ML signals:

Invoice/receipt: regex for “Invoice”, “Bill To”, “Ship To”, totals, tax, line items.

Tables: high density of aligned whitespace/columns; OCR output with many numeric tokens per line.

Handwritten: tesseract confidence low + many short tokens + image features (if you add a cheap vision embedding).

Forms/checkboxes: presence of “☐/☑” in OCR, or many single-character fields; or vision OCR hints.

Comment boxes: keywords like “Comment”, “Author”, “Response”, timestamps, or specific PDF annotations if you later extract them.

Store these as page metadata (even just JSON) and feed them into summarization.

Step 6 — Solve “mixed formats stitched together” by segmenting long PDFs

For longform, stitched PDFs: treat the document as a sequence of pages and segment it into “runs” of similar pages:

Create per-page embeddings from extracted text (you already embed chunks later, but you can do a page-level embedding too).

Cluster / change-point detect to split into sections (sudden shifts in embedding similarity + page-type labels).

Summarize per segment, then compose a document summary:

“This PDF contains: [segment summaries]”

include a short “table of contents” of detected segments and archetypes.

This is the most reliable way to produce “representative summaries” across mixed media.