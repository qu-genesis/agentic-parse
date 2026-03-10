#!/usr/bin/env python3
"""Generate a self-contained HTML document viewer (summaries only — no entity/relationship data).

Usage:
    uv run python scripts/build_viewer_lite.py [--workspace ./workspace]

Output: workspace/viewer_lite.html  (open directly in any browser)
"""

import argparse
import json
import os
from pathlib import Path


def _parse_summary(text: str) -> tuple[dict | None, str]:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed, stripped
    except json.JSONDecodeError:
        pass
    if "\n---\n" in stripped:
        json_part = stripped.split("\n---\n")[0].strip()
        try:
            parsed = json.loads(json_part)
            if isinstance(parsed, dict):
                return parsed, stripped
        except json.JSONDecodeError:
            pass
    return None, stripped


def load_data(workspace: Path) -> tuple[list[dict], dict, dict, list[dict]]:
    catalogue = workspace / "outputs" / "document_catalogue.jsonl"
    grouped_catalogue = workspace / "outputs" / "document_summary_catalogue.json"
    summaries_dir = workspace / "derived" / "summaries"

    docs_raw = [json.loads(l) for l in catalogue.read_text().splitlines() if l.strip()]

    summaries: dict[str, dict] = {}
    if summaries_dir.exists():
        for doc_dir in summaries_dir.iterdir():
            sf = doc_dir / "document.summary.txt"
            if sf.exists():
                raw = sf.read_text().strip()
                parsed, _ = _parse_summary(raw)
                summaries[doc_dir.name] = {"text": raw, "json": parsed}

    viewer_dir = workspace
    docs = []
    for d in docs_raw:
        doc_id = d["document_id"]
        raw_path = Path(d["path"])
        try:
            rel_pdf = os.path.relpath(raw_path, viewer_dir)
        except ValueError:
            rel_pdf = str(raw_path)

        s = summaries.get(doc_id, {})
        docs.append({
            "id":           doc_id,
            "name":         raw_path.name,
            "family":       d.get("doc_family", ""),
            "pages":        d.get("page_count"),
            "size":         d.get("size_bytes", 0),
            "summary":      s.get("text", ""),
            "summary_json": s.get("json"),
            "pdf":          rel_pdf,
        })

    docs.sort(key=lambda d: (not bool(d["summary"]), d["name"].lower()))
    grouped = {}
    if grouped_catalogue.exists():
        try:
            payload = json.loads(grouped_catalogue.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                grouped = payload
        except json.JSONDecodeError:
            grouped = {}

    # Build entity → [doc_id] map from summary_json key_entities
    entity_map: dict[str, list[str]] = {}
    for doc in docs:
        sj = doc.get("summary_json") or {}
        entities = sj.get("key_entities", []) if isinstance(sj, dict) else []
        for ent in entities:
            ent = str(ent).strip()
            if not ent:
                continue
            entity_map.setdefault(ent, []).append(doc["id"])

    registry: list[dict] = []
    registry_path = workspace / "outputs" / "entity_registry.json"
    if registry_path.exists():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                registry = data
        except (json.JSONDecodeError, OSError):
            registry = []

    return docs, grouped, entity_map, registry


# ── HTML template ──────────────────────────────────────────────────────────────

def generate_html(docs: list[dict], grouped_catalogue: dict, entity_map: dict, registry: list[dict]) -> str:
    data_json = json.dumps(docs, ensure_ascii=False, separators=(",", ":"))
    grouped_json = json.dumps(grouped_catalogue, ensure_ascii=False, separators=(",", ":"))
    entity_json = json.dumps(
        {k: v for k, v in sorted(entity_map.items())},
        ensure_ascii=False, separators=(",", ":")
    )
    registry_json = json.dumps(registry, ensure_ascii=False, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Document Viewer</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --sidebar-w:240px;
  --divider-w:5px;
  --accent:#4f46e5;
  --sidebar-bg:#1e293b;
  --sidebar-hover:#2d3f55;
  --sidebar-active:#334a66;
  --bg:#f0f2f7;
  --card-bg:#fff;
  --border:#e2e8f0;
  --text:#0f172a;
  --muted:#64748b;
  --light:#94a3b8;
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
}}
body{{font-family:var(--font);background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden;user-select:none}}

/* ── Sidebar ── */
#sidebar{{
  width:var(--sidebar-w);min-width:var(--sidebar-w);max-width:var(--sidebar-w);
  background:var(--sidebar-bg);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;
}}
#sidebar-header{{padding:16px 14px 10px;border-bottom:1px solid #334155}}
#sidebar-header h1{{color:#f1f5f9;font-size:14px;font-weight:700;letter-spacing:.2px;margin-bottom:8px}}
#doc-search{{
  width:100%;padding:6px 10px;border-radius:6px;border:1px solid #334155;
  background:#0f172a;color:#e2e8f0;font-size:12.5px;outline:none;
}}
#doc-search:focus{{border-color:var(--accent)}}
#doc-list{{flex:1;overflow-y:auto;padding:6px}}
#doc-list::-webkit-scrollbar{{width:4px}}
#doc-list::-webkit-scrollbar-thumb{{background:#334155;border-radius:4px}}
.doc-item{{padding:9px 10px;border-radius:7px;cursor:pointer;margin-bottom:2px;transition:background .1s}}
.doc-item:hover{{background:var(--sidebar-hover)}}
.doc-item.active{{background:var(--sidebar-active);border-left:3px solid var(--accent);padding-left:7px}}
.doc-item-name{{font-size:12px;color:#e2e8f0;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.4}}
.doc-item-meta{{display:flex;gap:5px;margin-top:3px;flex-wrap:wrap}}
.dbadge{{font-size:10px;padding:1px 5px;border-radius:8px;font-weight:500}}
.dbadge.sum{{background:#1e3a5f;color:#7dd3fc}}
.dbadge.nosum{{background:#3b1f1f;color:#f87171}}
.dbadge.fam{{background:#334155;color:#94a3b8}}

/* ── Content area ── */
#content-area{{flex:1;display:flex;overflow:hidden;min-width:0}}

/* ── Left panel (summary) ── */
#left-panel{{
  overflow-y:auto;display:flex;flex-direction:column;min-width:280px;
  background:var(--bg);
}}
#left-panel::-webkit-scrollbar{{width:6px}}
#left-panel::-webkit-scrollbar-thumb{{background:#c1c9d6;border-radius:4px}}

/* ── Drag divider ── */
#divider{{
  width:var(--divider-w);min-width:var(--divider-w);background:#dde1e9;
  cursor:col-resize;flex-shrink:0;position:relative;transition:background .15s;
}}
#divider:hover,#divider.dragging{{background:var(--accent)}}
#divider::after{{
  content:'';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  width:1px;height:32px;background:currentColor;opacity:.3;border-radius:1px;
}}

/* ── Right panel (PDF) ── */
#right-panel{{
  display:flex;flex-direction:column;overflow:hidden;min-width:0;background:#525659;
}}
#right-panel.hidden{{display:none}}
#pdf-toolbar{{
  display:flex;align-items:center;gap:8px;padding:7px 12px;
  background:#3c3f41;border-bottom:1px solid #2a2a2a;flex-shrink:0;
}}
#pdf-title{{
  font-size:12px;color:#c9d1d9;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;flex:1;font-weight:500;
}}
.pdf-btn{{
  padding:4px 10px;border-radius:5px;font-size:11.5px;cursor:pointer;border:none;
  font-weight:500;white-space:nowrap;transition:background .1s;
}}
#btn-new-tab{{background:#4f46e5;color:white}}
#btn-new-tab:hover{{background:#4338ca}}
#btn-hide-pdf{{background:#3b3f41;color:#94a3b8;border:1px solid #555}}
#btn-hide-pdf:hover{{background:#4a4e51;color:#e2e8f0}}
#pdf-frame{{flex:1;border:none;width:100%;display:block;background:#525659}}
#pdf-placeholder{{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:12px;color:#6b7280;
}}
#pdf-placeholder svg{{opacity:.4}}
#pdf-placeholder p{{font-size:13px;text-align:center;max-width:280px;line-height:1.5}}
#pdf-placeholder small{{font-size:11px;color:#9ca3af;text-align:center;max-width:280px;line-height:1.4}}

/* ── Show PDF button ── */
#btn-show-pdf{{
  display:none;position:fixed;bottom:18px;right:18px;
  padding:8px 14px;border-radius:8px;background:var(--accent);color:white;
  font-size:13px;font-weight:600;border:none;cursor:pointer;
  box-shadow:0 2px 8px rgba(79,70,229,.4);z-index:100;
}}
#btn-show-pdf.visible{{display:block}}

/* ── Welcome ── */
#welcome{{
  flex:1;display:flex;align-items:center;justify-content:center;
  flex-direction:column;gap:10px;color:var(--muted);
}}
#welcome p{{font-size:14px}}

/* ── Doc view ── */
#doc-view{{display:none;flex-direction:column;flex:1}}
#doc-header{{background:var(--card-bg);border-bottom:1px solid var(--border);padding:16px 22px 12px}}
#doc-title{{font-size:15px;font-weight:700;color:var(--text);line-height:1.3;word-break:break-word}}
#doc-chips{{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}}
.chip{{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:500;border:1px solid var(--border);color:var(--muted);background:var(--bg)}}
.chip.family{{background:#f0fdf4;color:#16a34a;border-color:#bbf7d0}}
.chip.pages{{background:#eff6ff;color:#2563eb;border-color:#bfdbfe}}
.chip.size{{background:#fefce8;color:#d97706;border-color:#fde68a}}
#doc-content{{padding:16px 22px;display:flex;flex-direction:column;gap:16px}}

/* ── Cards ── */
.card{{background:var(--card-bg);border-radius:10px;border:1px solid var(--border);overflow:hidden}}
.card-header{{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}}
.card-title{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--muted)}}
.card-body{{padding:16px}}
#summary-text{{font-size:13.5px;line-height:1.75;color:#1e293b;white-space:pre-line;user-select:text}}
#summary-structured{{user-select:text}}
#no-summary{{font-size:13px;color:var(--muted);font-style:italic}}
.sum-type{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:#7c3aed;
  margin-bottom:10px;padding:2px 8px;background:#f5f3ff;border-radius:4px;display:inline-block}}
.sum-purpose{{font-size:13.5px;line-height:1.7;color:#1e293b;margin-bottom:12px}}
.sum-section{{margin-bottom:12px}}
.sum-label{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  color:#475569;margin-bottom:4px}}
.sum-label.warn{{color:#b45309}}
.sum-list{{padding-left:16px;margin:0;list-style:disc}}
.sum-list li{{font-size:12.5px;line-height:1.6;color:#334155;padding:1px 0}}
.sum-toc{{display:flex;flex-direction:column;gap:5px}}
.toc-row{{display:flex;gap:8px;align-items:baseline}}
.toc-num{{font-size:11px;font-weight:700;color:#7c3aed;min-width:16px;flex-shrink:0}}
.toc-text{{font-size:12.5px;line-height:1.5;color:#334155}}
.toc-pages{{font-size:11px;color:#94a3b8}}

/* ── Grouped catalogue ── */
.cat-group{{border:1px solid #edf1f8;border-radius:10px;padding:14px 16px;background:#fafcff}}
.cat-group-head{{display:flex;justify-content:space-between;gap:8px;align-items:baseline}}
.cat-group-label{{font-size:16px;font-weight:700;color:#1e293b;text-transform:capitalize}}
.cat-group-count{{font-size:13px;color:#64748b;white-space:nowrap}}
.cat-group-desc{{margin-top:6px;font-size:13.5px;color:#475569;line-height:1.6}}
.cat-subgroup{{margin-top:10px}}
.cat-subgroup-label{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}}
.cat-subgroup-label.hue-0{{color:#4f46e5}}
.cat-subgroup-label.hue-1{{color:#0891b2}}
.cat-subgroup-label.hue-2{{color:#d97706}}
.cat-subgroup-label.hue-3{{color:#be185d}}
.cat-subgroup-label.hue-4{{color:#059669}}
.cat-subgroup-label.hue-5{{color:#7c3aed}}
.cat-docs{{display:flex;flex-wrap:wrap;gap:6px}}
.cat-doc-btn{{padding:4px 10px;border-radius:999px;border:1px solid #cbd5e1;background:#fff;color:#334155;font-size:13px;cursor:pointer;line-height:1.4}}
.cat-doc-btn:hover{{border-color:#818cf8;color:#312e81;background:#eef2ff}}
.cat-doc-btn.active{{background:#4f46e5;color:#fff;border-color:#4f46e5}}

/* ── Catalogue page ── */
#catalogue-page{{
  flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0;
}}
#catalogue-page-header{{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 24px;background:var(--card-bg);border-bottom:1px solid var(--border);
  flex-shrink:0;gap:12px;
}}
#catalogue-page-title{{font-size:20px;font-weight:700;color:var(--text)}}
#catalogue-filter-controls{{display:flex;align-items:center;gap:8px}}
#entity-select{{
  padding:5px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--card-bg);color:var(--text);font-size:12.5px;
  max-width:220px;cursor:pointer;outline:none;
}}
#entity-select:focus{{border-color:var(--accent)}}
#btn-entity-open{{
  padding:5px 12px;border-radius:6px;border:none;background:var(--accent);
  color:#fff;font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap;
}}
#btn-entity-open:hover{{background:#4338ca}}
#btn-entity-open:disabled{{background:#c7d2fe;cursor:default}}
#catalogue-page-body{{
  flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:14px;
}}
#catalogue-page-body::-webkit-scrollbar{{width:6px}}
#catalogue-page-body::-webkit-scrollbar-thumb{{background:#c1c9d6;border-radius:4px}}
.cat-group.dimmed{{opacity:0.25;pointer-events:none}}
.cat-doc-btn.entity-match{{border-color:var(--accent);color:var(--accent);background:#eef2ff;font-weight:600}}

/* ── Top nav ── */
#top-nav{{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;height:44px;min-height:44px;
  background:var(--sidebar-bg);border-bottom:2px solid #0f172a;
  flex-shrink:0;
}}
#nav-title{{color:#f1f5f9;font-size:14px;font-weight:700;letter-spacing:.2px}}
#nav-tabs{{display:flex;gap:4px}}
.nav-tab{{
  padding:5px 16px;border-radius:6px;border:none;cursor:pointer;
  font-size:13px;font-weight:600;background:transparent;color:#94a3b8;
  transition:background .1s,color .1s;
}}
.nav-tab:hover{{background:#2d3f55;color:#e2e8f0}}
.nav-tab.active{{background:var(--accent);color:#fff}}
</style>
</head>
<body>

<!-- Top nav -->
<div id="top-nav">
  <span id="nav-title">Document Viewer</span>
  <div id="nav-tabs">
    <button class="nav-tab active" id="tab-catalogue" onclick="showPage('catalogue')">Catalogue</button>
    <button class="nav-tab" id="tab-viewer" onclick="showPage('viewer')">Viewer</button>
  </div>
</div>

<!-- Page 1: Catalogue -->
<div id="catalogue-page" style="flex:1;min-height:0;flex-direction:column">
  <div id="catalogue-page-header">
    <span id="catalogue-page-title">Document Catalogue</span>
    <div id="catalogue-filter-controls">
      <select id="entity-select" onchange="onEntityChange()">
        <option value="">All entities</option>
      </select>
      <button id="btn-entity-open" onclick="openEntityDoc()" disabled>→ Open</button>
    </div>
  </div>
  <div id="catalogue-page-body">
    <div id="catalogue-page-empty" style="display:none;color:var(--muted);font-style:italic;font-size:13px">
      No catalogue groups generated yet. Run the summarize stage to build it.
    </div>
    <div id="catalogue-page-list"></div>
  </div>
</div>

<!-- Page 2: Viewer -->
<div id="viewer-page" style="display:none;flex:1;min-height:0;overflow:hidden;flex-direction:row">

<!-- Sidebar -->
<div id="sidebar">
  <div id="sidebar-header">
    <h1>Document Viewer</h1>
    <input id="doc-search" type="text" placeholder="Search documents…" oninput="filterDocs()">
  </div>
  <div id="doc-list"></div>
</div>

<!-- Content area -->
<div id="content-area">

  <!-- Left: summary -->
  <div id="left-panel" style="width:50%">
    <div id="welcome">
      <svg width="40" height="40" fill="none" viewBox="0 0 24 24" stroke="#94a3b8">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
          d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
      </svg>
      <p>Select a document to review</p>
    </div>

    <div id="doc-view">
      <div id="doc-header">
        <div id="doc-title"></div>
        <div id="doc-chips"></div>
      </div>
      <div id="doc-content">
        <div class="card">
          <div class="card-header"><span class="card-title">Summary</span></div>
          <div class="card-body">
            <div id="summary-structured" style="display:none"></div>
            <div id="summary-text" style="display:none"></div>
            <div id="no-summary" style="display:none">No summary generated for this document.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Drag divider -->
  <div id="divider"></div>

  <!-- Right: PDF viewer -->
  <div id="right-panel" style="flex:1">
    <div id="pdf-toolbar">
      <span id="pdf-title">No document selected</span>
      <button class="pdf-btn" id="btn-new-tab" onclick="openPdfTab()" style="display:none">↗ Open in tab</button>
      <button class="pdf-btn" id="btn-hide-pdf" onclick="togglePdf()">Hide PDF</button>
    </div>
    <div id="pdf-placeholder">
      <svg width="40" height="40" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
          d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/>
      </svg>
      <p>Select a document to view the original PDF</p>
      <small>If the PDF doesn't appear, use "Open in tab" or try Safari/Firefox for best local file support.</small>
    </div>
    <iframe id="pdf-frame" style="display:none"></iframe>
  </div>

</div>

<button id="btn-show-pdf" onclick="togglePdf()">Show PDF</button>

</div><!-- end #viewer-page -->

<script>
const DOCS = {data_json};
const CATALOGUE = {grouped_json};
const ENTITY_MAP = {entity_json};
const ENTITIES = Object.keys(ENTITY_MAP).sort((a,b)=>a.localeCompare(b));
const DOC_BY_ID = Object.fromEntries(DOCS.map(d => [d.id, d]));
const ENTITY_REGISTRY = {registry_json};
const HAS_REGISTRY = ENTITY_REGISTRY.length > 0;
const PEOPLE = ENTITY_REGISTRY.filter(e=>e.kind==='person').sort((a,b)=>b.mention_count-a.mention_count||a.canonical_name.localeCompare(b.canonical_name));
const ORGS = ENTITY_REGISTRY.filter(e=>e.kind==='organization').sort((a,b)=>b.mention_count-a.mention_count||a.canonical_name.localeCompare(b.canonical_name));
const REGISTRY_BY_DOC = {{}};
ENTITY_REGISTRY.forEach(e=>e.document_ids.forEach(id=>{{(REGISTRY_BY_DOC[id]=REGISTRY_BY_DOC[id]||[]).push(e)}}));

let currentDocIdx = -1;
let pdfVisible = true;
let currentPdfPath = null;

// ── Page navigation ────────────────────────────────────────────────────────────
function showPage(page) {{
  document.getElementById('catalogue-page').style.display = page==='catalogue' ? 'flex' : 'none';
  document.getElementById('viewer-page').style.display = page==='viewer' ? 'flex' : 'none';
  document.getElementById('tab-catalogue').classList.toggle('active', page==='catalogue');
  document.getElementById('tab-viewer').classList.toggle('active', page==='viewer');
}}

// ── Sidebar ────────────────────────────────────────────────────────────────────
function fmtSize(b){{ return b>1048576?(b/1048576).toFixed(1)+" MB":(b/1024).toFixed(0)+" KB"; }}

function renderSidebar(docs){{
  const list=document.getElementById("doc-list");
  list.innerHTML="";
  docs.forEach(doc=>{{
    const origIdx=DOCS.indexOf(doc);
    const el=document.createElement("div");
    el.className="doc-item"+(origIdx===currentDocIdx?" active":"");
    el.dataset.idx=origIdx;
    el.onclick=()=>selectDoc(origIdx);
    const hasSum=!!doc.summary;
    el.innerHTML=`
      <div class="doc-item-name" title="${{esc(doc.name)}}">${{esc(doc.name)}}</div>
      <div class="doc-item-meta">
        <span class="dbadge fam">${{esc(doc.family)}}</span>
        <span class="dbadge ${{hasSum?"sum":"nosum"}}">${{hasSum?"summarised":"no summary"}}</span>
      </div>`;
    list.appendChild(el);
  }});
}}

function filterDocs(){{
  const q=document.getElementById("doc-search").value.toLowerCase();
  renderSidebar(q?DOCS.filter(d=>d.name.toLowerCase().includes(q)):DOCS);
}}

// ── Document selection ─────────────────────────────────────────────────────────
function selectDoc(idx){{
  currentDocIdx=idx;
  const doc=DOCS[idx];

  document.getElementById("welcome").style.display="none";
  document.getElementById("doc-view").style.display="flex";

  document.getElementById("doc-title").textContent=doc.name;
  document.getElementById("doc-chips").innerHTML=`
    <span class="chip family">${{esc(doc.family)}}</span>
    ${{doc.pages?`<span class="chip pages">${{doc.pages}} page${{doc.pages!==1?"s":""}}</span>`:""}}
    <span class="chip size">${{fmtSize(doc.size)}}</span>`;

  renderSummary(doc);
  loadPdf(doc);

  document.querySelectorAll(".doc-item").forEach(el=>
    el.classList.toggle("active",parseInt(el.dataset.idx)===idx));
}}

// ── PDF panel ──────────────────────────────────────────────────────────────────
function loadPdf(doc){{
  currentPdfPath=doc.pdf;
  const frame=document.getElementById("pdf-frame");
  const placeholder=document.getElementById("pdf-placeholder");
  document.getElementById("pdf-title").textContent=doc.name;
  document.getElementById("btn-new-tab").style.display="";

  if(doc.family==="pdf"){{
    const encodedSrc=doc.pdf.split("/").map(s=>encodeURIComponent(s)).join("/");
    frame.src=encodedSrc;
    frame.style.display="";
    placeholder.style.display="none";
  }} else {{
    frame.src="about:blank";
    frame.style.display="none";
    placeholder.querySelector("p").textContent="PDF preview is only available for PDF documents.";
    placeholder.style.display="flex";
  }}
}}

function openPdfTab(){{
  if(currentPdfPath) window.open(currentPdfPath,"_blank");
}}

function togglePdf(){{
  pdfVisible=!pdfVisible;
  document.getElementById("right-panel").classList.toggle("hidden",!pdfVisible);
  document.getElementById("divider").style.display=pdfVisible?"":"none";
  document.getElementById("btn-show-pdf").classList.toggle("visible",!pdfVisible);
}}

// ── Summary rendering ──────────────────────────────────────────────────────────
function renderSummary(doc){{
  const structured=document.getElementById("summary-structured");
  const plain=document.getElementById("summary-text");
  const noSum=document.getElementById("no-summary");
  structured.style.display="none";
  plain.style.display="none";
  noSum.style.display="none";

  if(doc.summary_json){{
    structured.innerHTML=buildSummaryHtml(doc.summary_json);
    structured.style.display="";
  }} else if(doc.summary){{
    plain.textContent=doc.summary;
    plain.style.display="";
  }} else{{
    noSum.style.display="";
  }}
}}

function buildSummaryHtml(s){{
  const docType=s.document_type_or_mix||(s.document_types_present||[]).join(", ")||"";
  const purpose=s.purpose||s.overall_purpose||"";
  const entities=s.key_entities||[];
  const dates=s.key_dates||(s.timeline||[]).map(t=>typeof t==="string"?t:JSON.stringify(t));
  const amounts=s.quantitative_facts||s.financial_facts||[];
  const uncertainties=s.uncertainties||[];
  const toc=s.table_of_contents||[];
  let h="";
  if(docType) h+=`<div class="sum-type">${{esc(docType)}}</div>`;
  if(purpose) h+=`<p class="sum-purpose">${{esc(purpose)}}</p>`;
  if(entities.length) h+=sumList("Entities",entities);
  if(dates.length) h+=sumList("Dates / Timeline",dates);
  if(amounts.length) h+=sumList("Amounts",amounts);
  if(toc.length) h+=sumToc(toc);
  if(uncertainties.length) h+=sumList("Uncertainties",uncertainties,true);
  return h;
}}

function sumList(label,items,warn=false){{
  return`<div class="sum-section">
    <div class="sum-label${{warn?" warn":""}}">${{label}}</div>
    <ul class="sum-list">${{items.map(i=>`<li>${{esc(String(i))}}</li>`).join("")}}</ul>
  </div>`;
}}

function sumToc(toc){{
  return`<div class="sum-section">
    <div class="sum-label">Sections</div>
    <div class="sum-toc">${{toc.map(t=>`
      <div class="toc-row">
        <span class="toc-num">${{t.section_index}}</span>
        <span class="toc-text">${{esc(t.one_sentence_summary||"")}}${{t.pages_or_range?` <span class="toc-pages">(${{esc(t.pages_or_range)}})</span>`:""}}</span>
      </div>`).join("")}}
    </div>
  </div>`;
}}

function catalogueGroupHtml(group,activeDocId,matchingIds=null,dimmed=false){{
  const subgroups=Array.isArray(group.subgroups)?group.subgroups:[];
  const groupedIds=new Set();
  const subgroupHtml=subgroups.map((sg,sgIdx)=>{{
    const ids=(Array.isArray(sg.document_ids)?sg.document_ids:[]).filter(id=>DOC_BY_ID[id]);
    ids.forEach(id=>groupedIds.add(id));
    if(!ids.length) return "";
    return `
      <div class="cat-subgroup">
        <div class="cat-subgroup-label hue-${{sgIdx%6}}">${{esc(sg.label||"related subgroup")}}</div>
        <div class="cat-docs">${{catalogueDocButtons(ids,activeDocId,matchingIds)}}</div>
      </div>`;
  }}).join("");

  const topLevelIds=(Array.isArray(group.document_ids)?group.document_ids:[])
    .filter(id=>DOC_BY_ID[id]&&!groupedIds.has(id));
  const topHue=subgroups.length%6;
  const topLevelHtml=topLevelIds.length?`
    <div class="cat-subgroup">
      <div class="cat-subgroup-label hue-${{topHue}}">Documents</div>
      <div class="cat-docs">${{catalogueDocButtons(topLevelIds,activeDocId,matchingIds)}}</div>
    </div>`:"";

  return `
    <div class="cat-group${{dimmed?' dimmed':''}}">
      <div class="cat-group-head">
        <span class="cat-group-label">${{esc(group.label||"uncategorized documents")}}</span>
        <span class="cat-group-count">${{Number(group.document_count||0).toLocaleString()}} docs</span>
      </div>
      ${{group.description?`<div class="cat-group-desc">${{esc(group.description)}}</div>`:""}}
      ${{subgroupHtml}}
      ${{topLevelHtml}}
    </div>`;
}}

function catalogueDocButtons(docIds,activeDocId,matchingIds=null){{
  return docIds.map(docId=>{{
    const doc=DOC_BY_ID[docId];
    if(!doc) return "";
    const activeClass=docId===activeDocId?" active":"";
    const matchClass=matchingIds&&matchingIds.has(docId)?" entity-match":"";
    return `<button class="cat-doc-btn${{activeClass}}${{matchClass}}" onclick="selectDocById('${{escJs(docId)}}')">${{esc(doc.name)}}</button>`;
  }}).join("");
}}

function selectDocById(docId){{
  const idx=DOCS.findIndex(d=>d.id===docId);
  if(idx>=0){{
    showPage('viewer');
    selectDoc(idx);
  }}
}}

// ── Catalogue page ─────────────────────────────────────────────────────────────
function initCataloguePage() {{
  const sel = document.getElementById('entity-select');
  ENTITIES.forEach(e => {{
    const opt = document.createElement('option');
    opt.value = e;
    opt.textContent = e;
    sel.appendChild(opt);
  }});
  renderCataloguePage(null);
}}

function renderCataloguePage(filterEntity) {{
  const listEl = document.getElementById('catalogue-page-list');
  const emptyEl = document.getElementById('catalogue-page-empty');
  const groups = (CATALOGUE && Array.isArray(CATALOGUE.groups)) ? CATALOGUE.groups : [];
  if (!groups.length) {{
    listEl.innerHTML = '';
    emptyEl.style.display = '';
    return;
  }}
  emptyEl.style.display = 'none';

  const matchingIds = filterEntity ? new Set(ENTITY_MAP[filterEntity] || []) : null;

  listEl.innerHTML = groups.map(group => {{
    const groupDocIds = Array.isArray(group.document_ids) ? group.document_ids : [];
    const hasMatch = !matchingIds || groupDocIds.some(id => matchingIds.has(id));
    const dimmed = !!(matchingIds && !hasMatch);
    return catalogueGroupHtml(group, null, matchingIds, dimmed);
  }}).join('');
}}

function onEntityChange() {{
  const entity = document.getElementById('entity-select').value;
  document.getElementById('btn-entity-open').disabled = !entity;
  renderCataloguePage(entity || null);
}}

function openEntityDoc() {{
  const entity = document.getElementById('entity-select').value;
  if (!entity) return;
  const docIds = ENTITY_MAP[entity] || [];
  if (!docIds.length) return;
  const idx = DOCS.findIndex(d => d.id === docIds[0]);
  if (idx >= 0) {{
    showPage('viewer');
    selectDoc(idx);
  }}
}}

// ── Draggable divider ──────────────────────────────────────────────────────────
(function(){{
  const divider=document.getElementById("divider");
  const leftPanel=document.getElementById("left-panel");
  const contentArea=document.getElementById("content-area");
  let dragging=false, startX=0, startW=0;

  divider.addEventListener("mousedown",e=>{{
    dragging=true; startX=e.clientX; startW=leftPanel.offsetWidth;
    divider.classList.add("dragging");
    document.body.style.cursor="col-resize";
    e.preventDefault();
  }});
  document.addEventListener("mousemove",e=>{{
    if(!dragging) return;
    const totalW=contentArea.offsetWidth;
    const newW=Math.min(Math.max(startW+(e.clientX-startX),200),totalW-200);
    leftPanel.style.width=newW+"px";
  }});
  document.addEventListener("mouseup",()=>{{
    if(!dragging) return;
    dragging=false;
    divider.classList.remove("dragging");
    document.body.style.cursor="";
  }});
}})();

function esc(s){{
  if(!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

function escJs(s){{
  if(!s) return "";
  return String(s).replace(/'/g,"\\'");
}}

// Init
initCataloguePage();
renderSidebar(DOCS);
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build self-contained HTML document viewer (summaries only)")
    parser.add_argument("--workspace", type=Path, default=Path("./workspace"))
    args = parser.parse_args()

    print("Loading pipeline outputs…")
    docs, grouped_catalogue, entity_map, registry = load_data(args.workspace)
    summarised = sum(1 for d in docs if d["summary"])
    grouped_count = int(grouped_catalogue.get("group_count", 0)) if isinstance(grouped_catalogue, dict) else 0
    print(f"  {len(docs)} documents, {summarised} with summaries, {grouped_count} catalogue groups")

    html = generate_html(docs, grouped_catalogue, entity_map, registry)
    out = args.workspace / "viewer_lite.html"
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size // 1024
    print(f"  Written → {out}  ({size_kb} KB)")
    print(f"  Open in browser:  open {out}")


if __name__ == "__main__":
    main()
