"""
Scalability report: token costs and inference speed for agentic-parse
projected from the current corpus to 16 GB.

Usage:
    uv run python scripts/scalability_report.py
"""
from __future__ import annotations
import math

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OBSERVED CORPUS BASELINES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CORPUS = dict(
    docs          = 10,
    bytes         = 314_888_363,
    pages         = 1_446,
    chunks        = 1_544,
    avg_chunk_tok = 219,       # tokens per chunk (measured)
)
# Tier distribution from actual pipeline run
TIER_DIST = dict(
    tier0  = 137 / 1446,       # 9.5%  — native text layer (all 3 backends)
    tier2a = 798 / 1446,       # 55.2% — LLM text cleanup (gpt-4o-mini)
    tier2b = 511 / 1446,       # 35.3% — vision OCR     (gpt-4o-mini)
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRICING  (USD per 1M tokens, as of 2025)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRICE = {
    # model: (input $/1M, output $/1M)
    "gpt-4o-mini":              (0.15,  0.60),   # text + vision (default OPENAI_MODEL)
    "gpt-4o":                   (2.50, 10.00),   # alt vision model
    "text-embedding-3-small":   (0.02,  0.00),   # embeddings
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PER-UNIT TOKEN BUDGETS  (measured or estimated from pipeline + tier-compare)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOKENS = dict(
    # tier-2b vision OCR (measured from tier_compare on this corpus, gpt-4o-mini vision)
    tier2b_in_per_page   = 825,
    tier2b_out_per_page  = 471,   # avg across 4 pages

    # tier-2a LLM cleanup  (system+user prompt ~300 tok + OCR text ~1200 tok; output capped 1200)
    tier2a_in_per_page   = 1_500,
    tier2a_out_per_page  = 1_000,

    # summarize — small doc (<20 pages): retrieval context + prompt → one JSON call
    summarize_small_in   = 3_000,
    summarize_small_out  =   500,

    # summarize — large doc (>20 pages): ~10 segment calls + 1 composed call
    summarize_large_in   = 22_000,
    summarize_large_out  =  4_700,

    # entity extraction — 1 LLM call per chunk (gpt-4o-mini)
    entity_in_per_chunk  = 1_400,
    entity_out_per_chunk = 1_400,

    # embeddings (text-embedding-3-small)
    embed_tok_per_chunk  = 219,   # measured avg
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LATENCY ESTIMATES  (seconds per unit, single-threaded baseline)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LATENCY = dict(
    tier0_sec_per_page   = 0.05,   # local, sub-100ms
    tier1_sec_per_page   = 3.0,    # tesseract, 4-rotation search
    tier2a_sec_per_page  = 1.5,    # gpt-4o-mini API call
    tier2b_sec_per_page  = 3.0,    # gpt-4o-mini vision (image encode + API)
    embed_sec_per_chunk  = 0.05,   # batched; ~50ms per chunk equivalent
    summarize_sec_per_doc= 15.0,   # avg small+large mix
    entity_sec_per_chunk = 2.0,    # gpt-4o-mini JSON call per chunk
)

# parallelism knobs (from pipeline config defaults)
PARALLELISM = dict(
    extract_workers  =  4,   # --workers flag
    entity_workers   = 16,   # ENTITIES_MAX_WORKERS
    embed_batch      = 32,   # approximate embedding batch size
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cost(model: str, in_tok: int, out_tok: int) -> float:
    ip, op = PRICE[model]
    return in_tok / 1e6 * ip + out_tok / 1e6 * op

def fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f} min"
    return f"{seconds/3600:.1f} hrs"

def fmt_usd(v: float) -> str:
    if v < 0.01:
        return f"${v:.5f}"
    if v < 1:
        return f"${v:.4f}"
    return f"${v:,.2f}"

def bar(v: float, max_v: float, width: int = 30) -> str:
    filled = round(v / max_v * width) if max_v else 0
    return "█" * filled + "░" * (width - filled)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCALING PROJECTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MB = 1024 ** 2
GB = 1024 ** 3

pages_per_byte  = CORPUS["pages"]  / CORPUS["bytes"]
chunks_per_page = CORPUS["chunks"] / CORPUS["pages"]
docs_per_byte   = CORPUS["docs"]   / CORPUS["bytes"]

# fraction of large docs (>20 pages) — rough assumption based on corpus
LARGE_DOC_FRAC = 0.60

SCALE_POINTS_GB = [0.001 * CORPUS["bytes"] / GB, 0.01 * CORPUS["bytes"] / GB,
                   0.1, 0.5, 1, 4, 8, 16]
SCALE_POINTS_GB = sorted(set([round(x, 4) for x in SCALE_POINTS_GB] + [0.1, 0.5, 1, 4, 8, 16]))

def project(gb: float) -> dict:
    total_bytes  = gb * GB
    pages        = total_bytes * pages_per_byte
    chunks       = pages * chunks_per_page
    docs         = total_bytes * docs_per_byte

    t0_pages     = pages * TIER_DIST["tier0"]
    t2a_pages    = pages * TIER_DIST["tier2a"]
    t2b_pages    = pages * TIER_DIST["tier2b"]

    small_docs   = docs * (1 - LARGE_DOC_FRAC)
    large_docs   = docs * LARGE_DOC_FRAC

    # ── token counts ───────────────────────────────────────────────────────────
    tok_tier2a_in  = t2a_pages * TOKENS["tier2a_in_per_page"]
    tok_tier2a_out = t2a_pages * TOKENS["tier2a_out_per_page"]
    tok_tier2b_in  = t2b_pages * TOKENS["tier2b_in_per_page"]
    tok_tier2b_out = t2b_pages * TOKENS["tier2b_out_per_page"]

    tok_sum_in  = (small_docs * TOKENS["summarize_small_in"]
                   + large_docs * TOKENS["summarize_large_in"])
    tok_sum_out = (small_docs * TOKENS["summarize_small_out"]
                   + large_docs * TOKENS["summarize_large_out"])

    tok_ent_in  = chunks * TOKENS["entity_in_per_chunk"]
    tok_ent_out = chunks * TOKENS["entity_out_per_chunk"]

    tok_embed   = chunks * TOKENS["embed_tok_per_chunk"]

    total_in  = tok_tier2a_in + tok_tier2b_in + tok_sum_in + tok_ent_in
    total_out = tok_tier2a_out + tok_tier2b_out + tok_sum_out + tok_ent_out

    # ── costs ──────────────────────────────────────────────────────────────────
    c_tier2a = cost("gpt-4o-mini", tok_tier2a_in, tok_tier2a_out)
    c_tier2b = cost("gpt-4o-mini", tok_tier2b_in, tok_tier2b_out)
    c_sum    = cost("gpt-4o-mini", tok_sum_in, tok_sum_out)
    c_ent    = cost("gpt-4o-mini", tok_ent_in, tok_ent_out)
    c_embed  = cost("text-embedding-3-small", tok_embed, 0)
    c_total  = c_tier2a + c_tier2b + c_sum + c_ent + c_embed

    # ── wall-clock time (with parallelism) ─────────────────────────────────────
    ew = PARALLELISM["extract_workers"]
    enw = PARALLELISM["entity_workers"]

    t_extract = (
        t0_pages  * LATENCY["tier0_sec_per_page"]  / ew
        + t2a_pages * LATENCY["tier2a_sec_per_page"] / ew
        + t2b_pages * LATENCY["tier2b_sec_per_page"] / ew
    )
    t_embed   = chunks * LATENCY["embed_sec_per_chunk"] / PARALLELISM["embed_batch"]
    t_sum     = docs * LATENCY["summarize_sec_per_doc"]   # sequential per doc
    t_entity  = chunks * LATENCY["entity_sec_per_chunk"]  / enw
    t_total   = t_extract + t_embed + t_sum + t_entity   # stages run sequentially

    return dict(
        gb=gb, pages=pages, chunks=chunks, docs=docs,
        t0_pages=t0_pages, t2a_pages=t2a_pages, t2b_pages=t2b_pages,
        total_in=total_in, total_out=total_out,
        c_tier2a=c_tier2a, c_tier2b=c_tier2b, c_sum=c_sum, c_ent=c_ent,
        c_embed=c_embed, c_total=c_total,
        t_extract=t_extract, t_embed=t_embed, t_sum=t_sum, t_entity=t_entity,
        t_total=t_total,
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRINT REPORT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

W = 82
print()
print("═" * W)
print("  AGENTIC-PARSE  ·  SCALABILITY REPORT")
print("═" * W)

print(f"""
  Models
  ──────
  Text extraction (tier-2a LLM cleanup)  : gpt-4o-mini  ($0.15/$0.60 per 1M in/out)
  Vision OCR      (tier-2b)              : gpt-4o-mini  ($0.15/$0.60 per 1M in/out)
  Summarization                          : gpt-4o-mini  ($0.15/$0.60 per 1M in/out)
  Entity extraction                      : gpt-4o-mini  ($0.15/$0.60 per 1M in/out)
  Embeddings                             : text-embedding-3-small  ($0.02 per 1M tok)

  Observed corpus baseline
  ────────────────────────
  Documents : {CORPUS['docs']:,}
  Size      : {CORPUS['bytes']/MB:.1f} MB
  Pages     : {CORPUS['pages']:,}   ({CORPUS['pages']/CORPUS['bytes']*MB:.1f} pages/MB)
  Chunks    : {CORPUS['chunks']:,}   ({CORPUS['chunks']/CORPUS['pages']:.2f} chunks/page, avg {CORPUS['avg_chunk_tok']} tok/chunk)

  Tier distribution (measured)
  ────────────────────────────
  tier-0  (native text layer)  : {TIER_DIST['tier0']*100:.1f}%
  tier-2a (LLM text cleanup)   : {TIER_DIST['tier2a']*100:.1f}%
  tier-2b (vision OCR, gpt-4o-mini) : {TIER_DIST['tier2b']*100:.1f}%

  Parallelism (current defaults)
  ──────────────────────────────
  extract-text workers : {PARALLELISM['extract_workers']}   (--workers flag)
  entity workers       : {PARALLELISM['entity_workers']}   (ENTITIES_MAX_WORKERS env)
  embedding batch      : ~{PARALLELISM['embed_batch']} chunks
""")

# ── per-page / per-chunk unit economics ────────────────────────────────────────
print("─" * W)
print("  UNIT ECONOMICS  (per page / per chunk)")
print("─" * W)

unit_rows = [
    ("tier-0 extract",    "page",  0.0,
     0.0,                             LATENCY["tier0_sec_per_page"]),
    ("tier-2a LLM cleanup", "page",
     cost("gpt-4o-mini", TOKENS["tier2a_in_per_page"], TOKENS["tier2a_out_per_page"]),
     TOKENS["tier2a_in_per_page"] + TOKENS["tier2a_out_per_page"],
     LATENCY["tier2a_sec_per_page"]),
    ("tier-2b vision OCR", "page",
     cost("gpt-4o-mini", TOKENS["tier2b_in_per_page"], TOKENS["tier2b_out_per_page"]),
     TOKENS["tier2b_in_per_page"] + TOKENS["tier2b_out_per_page"],
     LATENCY["tier2b_sec_per_page"]),
    ("embedding",         "chunk",
     cost("text-embedding-3-small", TOKENS["embed_tok_per_chunk"], 0),
     TOKENS["embed_tok_per_chunk"],
     LATENCY["embed_sec_per_chunk"]),
    ("entity extract",    "chunk",
     cost("gpt-4o-mini", TOKENS["entity_in_per_chunk"], TOKENS["entity_out_per_chunk"]),
     TOKENS["entity_in_per_chunk"] + TOKENS["entity_out_per_chunk"],
     LATENCY["entity_sec_per_chunk"]),
    ("summarize (small)", "doc",
     cost("gpt-4o-mini", TOKENS["summarize_small_in"], TOKENS["summarize_small_out"]),
     TOKENS["summarize_small_in"] + TOKENS["summarize_small_out"],
     LATENCY["summarize_sec_per_doc"] * 0.4),
    ("summarize (large)", "doc",
     cost("gpt-4o-mini", TOKENS["summarize_large_in"], TOKENS["summarize_large_out"]),
     TOKENS["summarize_large_in"] + TOKENS["summarize_large_out"],
     LATENCY["summarize_sec_per_doc"] * 1.5),
]

print(f"  {'Stage':<25} {'Unit':<6} {'Tokens':>8}  {'Cost':>10}  {'Latency':>10}")
print(f"  {'─'*25} {'─'*6} {'─'*8}  {'─'*10}  {'─'*10}")
for name, unit, usd, tok, lat in unit_rows:
    print(f"  {name:<25} {unit:<6} {tok:>8,.0f}  {fmt_usd(usd):>10}  {fmt_time(lat):>10}")

# ── scaling table ──────────────────────────────────────────────────────────────
print()
print("─" * W)
print("  SCALING PROJECTIONS  (gpt-4o-mini for all LLM stages)")
print("─" * W)
print(f"  {'Size':>7}  {'Pages':>8}  {'Chunks':>8}  {'Total cost':>12}  {'Wall clock':>12}  {'Tokens (M)':>11}")
print(f"  {'─'*7}  {'─'*8}  {'─'*8}  {'─'*12}  {'─'*12}  {'─'*11}")

p16 = None
for gb in SCALE_POINTS_GB:
    p = project(gb)
    marker = " ◄ current" if abs(gb - CORPUS["bytes"] / GB) < 0.001 else ""
    if gb == 16:
        p16 = p
        marker = " ◄ target"
    tok_total_m = (p["total_in"] + p["total_out"]) / 1e6
    print(
        f"  {gb:>6.3f}GB"
        f"  {p['pages']:>8,.0f}"
        f"  {p['chunks']:>8,.0f}"
        f"  {fmt_usd(p['c_total']):>12}"
        f"  {fmt_time(p['t_total']):>12}"
        f"  {tok_total_m:>10.1f}M"
        f"{marker}"
    )

# ── 16 GB breakdown ────────────────────────────────────────────────────────────
p = p16 or project(16)
print()
print("─" * W)
print("  16 GB COST BREAKDOWN")
print("─" * W)

cost_rows = [
    ("tier-2a LLM cleanup",    p["c_tier2a"], p["t2a_pages"],  "pages"),
    ("tier-2b vision OCR",     p["c_tier2b"], p["t2b_pages"],  "pages"),
    ("entity extraction",      p["c_ent"],    p["chunks"],     "chunks"),
    ("summarization",          p["c_sum"],    p["docs"],       "docs"),
    ("embeddings",             p["c_embed"],  p["chunks"],     "chunks"),
]
max_cost = max(r[1] for r in cost_rows)
print(f"  {'Stage':<25} {'Volume':>12}  {'Cost':>10}  {'Share':>6}  {'Bar'}")
print(f"  {'─'*25} {'─'*12}  {'─'*10}  {'─'*6}  {'─'*30}")
for name, c, vol, unit in cost_rows:
    share = c / p["c_total"] * 100
    print(f"  {name:<25} {vol:>11,.0f}{unit[0]}  {fmt_usd(c):>10}  {share:>5.1f}%  {bar(c, max_cost)}")
print(f"  {'─'*25} {'─'*12}  {'─'*10}  {'─'*6}")
print(f"  {'TOTAL':<25} {'':>12}  {fmt_usd(p['c_total']):>10}  {'100.0%':>6}")

# ── 16 GB time breakdown ────────────────────────────────────────────────────────
print()
print("─" * W)
print("  16 GB TIME BREAKDOWN  (current parallelism)")
print("─" * W)

time_rows = [
    ("extract-text",   p["t_extract"], f"{PARALLELISM['extract_workers']} workers"),
    ("chunk + embed",  p["t_embed"],   f"~{PARALLELISM['embed_batch']} per batch"),
    ("summarize",      p["t_sum"],     "sequential per doc"),
    ("entities",       p["t_entity"],  f"{PARALLELISM['entity_workers']} workers"),
]
max_t = max(r[1] for r in time_rows)
print(f"  {'Stage':<18} {'Time':>12}  {'Parallelism':<22}  {'Bar'}")
print(f"  {'─'*18} {'─'*12}  {'─'*22}  {'─'*30}")
for name, t, note in time_rows:
    print(f"  {name:<18} {fmt_time(t):>12}  {note:<22}  {bar(t, max_t)}")
print(f"  {'─'*18} {'─'*12}")
print(f"  {'TOTAL (serial)':<18} {fmt_time(p['t_total']):>12}")

# ── bottleneck analysis ────────────────────────────────────────────────────────
print()
print("─" * W)
print("  BOTTLENECK & SCALING NOTES")
print("─" * W)

# OpenAI rate limits for gpt-4o-mini (Tier 1): 2M TPM, 10k RPM
TPM_LIMIT   = 2_000_000
RPM_LIMIT   = 10_000
ent_rps     = PARALLELISM["entity_workers"]
t2b_tpm_16  = (p["t2b_pages"] * (TOKENS["tier2b_in_per_page"] + TOKENS["tier2b_out_per_page"])
                / max(p["t_extract"] / 60, 1))
ent_tpm_16  = (p["chunks"] * (TOKENS["entity_in_per_chunk"] + TOKENS["entity_out_per_chunk"])
                / max(p["t_entity"] / 60, 1))

print(f"""
  1. ENTITY EXTRACTION is the dominant cost driver at scale.
     At 16 GB: {fmt_usd(p['c_ent'])} ({p['c_ent']/p['c_total']*100:.0f}% of total) for {p['chunks']:,.0f} chunks.
     → Mitigation: raise ENTITIES_MAX_WORKERS, or skip low-value chunks
       (e.g. chunks with quality_score < 0.4).

  2. EXTRACT-TEXT is the dominant time driver.
     At 16 GB with 4 workers: {fmt_time(p['t_extract'])} just for OCR/text extraction.
     → Mitigation: increase --workers. Each doubling halves wall time.
       At 32 workers: ~{fmt_time(p['t_extract']/8)}.  At 64 workers: ~{fmt_time(p['t_extract']/16)}.

  3. OPENAI RATE LIMITS (gpt-4o-mini Tier 1: 2M TPM, 10k RPM)
     Entities at 16 workers generates ~{ent_rps} req/s = {ent_rps*60:,} RPM
     — already at {ent_rps*60/RPM_LIMIT*100:.0f}% of the 10k RPM limit.
     → Mitigation: add exponential backoff (already partially handled),
       or request higher rate limit tier from OpenAI.

  4. VISION OCR (tier-2b) costs {fmt_usd(p['c_tier2b'])} at 16 GB using gpt-4o-mini.
     If switched to gpt-4o: {fmt_usd(cost("gpt-4o", p["t2b_pages"]*TOKENS["tier2b_in_per_page"], p["t2b_pages"]*TOKENS["tier2b_out_per_page"]))} (~17× more expensive).
     → Keep gpt-4o-mini for vision; quality difference is marginal for typed docs.

  5. CACHING eliminates repeat costs. The pipeline caches all LLM responses
     by content hash. Duplicate or near-duplicate pages across documents
     (e.g. boilerplate headers/footers) are free after the first call.

  6. SUMMARIZATION scales with doc count, not page count (segmented path).
     At 16 GB ({p['docs']:,.0f} docs): {fmt_usd(p['c_sum'])} — modest cost, but
     {fmt_time(p['t_sum'])} wall clock with current sequential-per-doc design.
     → Mitigation: parallelize summarize across docs (trivial to add).
""")

# ── cost sensitivity ───────────────────────────────────────────────────────────
print("─" * W)
print("  COST SENSITIVITY  (16 GB — varying tier mix)")
print("─" * W)

scenarios = [
    ("All tier-0 (text-layer PDFs)",  1.0,  0.0,  0.0),
    ("50% t0 / 50% t2a",              0.5,  0.5,  0.0),
    ("Current mix (observed)",
     TIER_DIST["tier0"], TIER_DIST["tier2a"], TIER_DIST["tier2b"]),
    ("All tier-2a (LLM cleanup)",     0.0,  1.0,  0.0),
    ("All tier-2b (vision OCR)",      0.0,  0.0,  1.0),
]

total_pages_16 = project(16)["pages"]
base_non_extract = project(16)["c_ent"] + project(16)["c_sum"] + project(16)["c_embed"]

print(f"  {'Scenario':<40} {'Extract cost':>13}  {'Total cost':>12}")
print(f"  {'─'*40} {'─'*13}  {'─'*12}")
for label, t0f, t2af, t2bf in scenarios:
    t2a_p = total_pages_16 * t2af
    t2b_p = total_pages_16 * t2bf
    c_ex = (cost("gpt-4o-mini", t2a_p * TOKENS["tier2a_in_per_page"],
                                 t2a_p * TOKENS["tier2a_out_per_page"])
           + cost("gpt-4o-mini", t2b_p * TOKENS["tier2b_in_per_page"],
                                  t2b_p * TOKENS["tier2b_out_per_page"]))
    c_tot = c_ex + base_non_extract
    print(f"  {label:<40} {fmt_usd(c_ex):>13}  {fmt_usd(c_tot):>12}")

print()
print("═" * W)
print("  Note: All LLM costs assume gpt-4o-mini (default OPENAI_MODEL).")
print("  Tier-1 (Tesseract) is free but adds ~3s/page latency.")
print("  Wall-clock assumes API calls dominate; local CPU is negligible by comparison.")
print("═" * W)
print()
