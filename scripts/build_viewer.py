#!/usr/bin/env python3
"""Generate a self-contained HTML document viewer from pipeline outputs.

Usage:
    uv run python scripts/build_viewer.py [--workspace ./workspace]

Output: workspace/viewer.html  (open directly in any browser)
"""

import argparse
import json
import os
from pathlib import Path


def _parse_summary(text: str) -> tuple[dict | None, str]:
    """Try to parse summary text as JSON. Handles both pure-JSON and JSON+appended text.

    Returns (parsed_dict_or_None, raw_text).
    """
    stripped = text.strip()
    # Embedding-based summary: whole file is valid JSON
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed, stripped
    except json.JSONDecodeError:
        pass
    # Segmented summary: JSON followed by "\n\n---\n..." trailer
    if "\n---\n" in stripped:
        json_part = stripped.split("\n---\n")[0].strip()
        try:
            parsed = json.loads(json_part)
            if isinstance(parsed, dict):
                return parsed, stripped
        except json.JSONDecodeError:
            pass
    return None, stripped


def load_data(workspace: Path) -> list[dict]:
    catalogue  = workspace / "outputs" / "document_catalogue.jsonl"
    rels_file  = workspace / "outputs" / "relationships.jsonl"
    entities_dir = workspace / "outputs" / "entities"
    summaries_dir = workspace / "derived" / "summaries"

    docs_raw = [json.loads(l) for l in catalogue.read_text().splitlines() if l.strip()]

    rels_raw = []
    if rels_file.exists():
        rels_raw = [json.loads(l) for l in rels_file.read_text().splitlines() if l.strip()]

    entity_map: dict[str, dict] = {}
    for f in entities_dir.glob("*.json"):
        card = json.loads(f.read_text())
        entity_map[card["entity_id"]] = {
            "name": card["canonical_name"],
            "kind": card.get("kind", ""),
        }

    summaries: dict[str, dict] = {}
    if summaries_dir.exists():
        for doc_dir in summaries_dir.iterdir():
            sf = doc_dir / "document.summary.txt"
            if sf.exists():
                raw = sf.read_text().strip()
                parsed, _ = _parse_summary(raw)
                summaries[doc_dir.name] = {"text": raw, "json": parsed}

    rels_by_doc: dict[str, list] = {}
    for rel in rels_raw:
        doc_id = rel["document_id"]
        sm = entity_map.get(rel["subject_entity_id"], {})
        om = entity_map.get(rel["object_entity_id"], {})
        rels_by_doc.setdefault(doc_id, []).append({
            "s":  sm.get("name", rel["subject_entity_id"]),
            "sk": sm.get("kind", ""),
            "p":  rel["predicate"],
            "o":  om.get("name", rel["object_entity_id"]),
            "ok": om.get("kind", ""),
            "e":  (rel.get("evidence_excerpt") or "").strip(),
            "pg": rel.get("page_number"),
            "c":  round(rel.get("confidence", 0.0), 2),
        })

    # viewer.html lives in workspace/; compute relative path to each PDF from there
    viewer_dir = workspace

    docs = []
    for d in docs_raw:
        doc_id = d["document_id"]
        raw_path = Path(d["path"])
        name = raw_path.name
        # relative path from workspace/ to the PDF — used as iframe src
        try:
            rel_pdf = os.path.relpath(raw_path, viewer_dir)
        except ValueError:
            rel_pdf = str(raw_path)          # different drives on Windows

        s = summaries.get(doc_id, {})
        docs.append({
            "id":           doc_id,
            "name":         name,
            "family":       d.get("doc_family", ""),
            "pages":        d.get("page_count"),
            "size":         d.get("size_bytes", 0),
            "summary":      s.get("text", ""),
            "summary_json": s.get("json"),   # structured dict or None
            "pdf":          rel_pdf,
            "rels":         rels_by_doc.get(doc_id, []),
        })

    docs.sort(key=lambda d: (not bool(d["summary"]), d["name"].lower()))
    return docs


# ── HTML template ─────────────────────────────────────────────────────────────

def generate_html(docs: list[dict]) -> str:
    data_json = json.dumps(docs, ensure_ascii=False, separators=(",", ":"))
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
body{{font-family:var(--font);background:var(--bg);color:var(--text);height:100vh;display:flex;overflow:hidden;user-select:none}}

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
.doc-item-rels{{font-size:10.5px;color:#475569;margin-top:2px}}

/* ── Content area (everything right of sidebar) ── */
#content-area{{flex:1;display:flex;overflow:hidden;min-width:0}}

/* ── Left panel (summary + relationships) ── */
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

/* ── Show PDF button (when panel is hidden) ── */
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

/* ── Doc view (inside left panel) ── */
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

/* ── Relationships ── */
#rel-stats{{display:flex;gap:18px;padding:12px 16px;background:#f8fafc;border-bottom:1px solid var(--border);flex-wrap:wrap}}
.stat-val{{font-size:18px;font-weight:700;color:var(--text)}}
.stat-lbl{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
#rel-controls{{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
#pred-filter{{padding:6px 9px;border:1px solid var(--border);border-radius:6px;font-size:12.5px;color:var(--text);background:white;outline:none;min-width:160px;cursor:pointer}}
#pred-filter:focus{{border-color:var(--accent)}}
#rel-search{{padding:6px 9px;border:1px solid var(--border);border-radius:6px;font-size:12.5px;color:var(--text);background:white;outline:none;flex:1;min-width:130px}}
#rel-search:focus{{border-color:var(--accent)}}
#rel-count-label{{font-size:11.5px;color:var(--muted);margin-left:auto;white-space:nowrap}}
#rel-table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
thead th{{background:#f8fafc;padding:8px 12px;text-align:left;font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);border-bottom:2px solid var(--border);white-space:nowrap}}
tbody tr{{border-bottom:1px solid #f1f5f9;transition:background .07s}}
tbody tr:hover{{background:#f8fafc}}
tbody td{{padding:8px 12px;vertical-align:top;user-select:text}}
.entity-name{{font-weight:500;color:var(--text)}}
.entity-kind{{font-size:9.5px;color:var(--light)}}
.pred-pill{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}}
.evidence-text{{font-size:11.5px;color:var(--muted);line-height:1.5;max-width:240px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}}
.evidence-text.expanded{{-webkit-line-clamp:unset;overflow:visible}}
.page-cell{{color:var(--muted);text-align:center;white-space:nowrap}}
.conf-bar{{height:3px;border-radius:2px;background:#e2e8f0;width:36px;margin:0 auto}}
.conf-fill{{height:100%;border-radius:2px;background:var(--accent)}}
.conf-pct{{font-size:9.5px;color:var(--light);text-align:center;margin-top:1px}}
#no-rels{{padding:28px;text-align:center;color:var(--muted);font-size:13px}}
#pagination{{display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 16px;border-top:1px solid var(--border);background:#f8fafc}}
#pagination button{{padding:5px 11px;border-radius:5px;border:1px solid var(--border);background:white;font-size:12.5px;cursor:pointer;color:var(--text);transition:all .1s}}
#pagination button:hover:not(:disabled){{background:var(--accent);color:white;border-color:var(--accent)}}
#pagination button:disabled{{opacity:.4;cursor:default}}
#page-info{{font-size:12px;color:var(--muted);min-width:90px;text-align:center}}
</style>
</head>
<body>

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

  <!-- Left: summary + relationships -->
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

        <div class="card">
          <div class="card-header"><span class="card-title">Relationships</span></div>
          <div id="rel-stats">
            <div><div class="stat-val" id="stat-total">0</div><div class="stat-lbl">Total</div></div>
            <div><div class="stat-val" id="stat-predicates">0</div><div class="stat-lbl">Predicates</div></div>
            <div><div class="stat-val" id="stat-entities">0</div><div class="stat-lbl">Entities</div></div>
          </div>
          <div id="rel-controls">
            <select id="pred-filter" onchange="applyFilter()"><option value="">All predicates</option></select>
            <input id="rel-search" type="text" placeholder="Search…" oninput="applyFilter()">
            <span id="rel-count-label"></span>
          </div>
          <div id="rel-table-wrap">
            <div id="no-rels" style="display:none">No relationships found.</div>
            <table id="rel-table" style="display:none">
              <thead><tr>
                <th style="width:17%">Subject</th>
                <th style="width:13%">Predicate</th>
                <th style="width:17%">Object</th>
                <th>Evidence</th>
                <th style="width:44px">Page</th>
                <th style="width:50px">Conf</th>
              </tr></thead>
              <tbody id="rel-tbody"></tbody>
            </table>
          </div>
          <div id="pagination">
            <button id="btn-prev" onclick="changePage(-1)">← Prev</button>
            <span id="page-info"></span>
            <button id="btn-next" onclick="changePage(1)">Next →</button>
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

<!-- Show PDF button (when panel hidden) -->
<button id="btn-show-pdf" onclick="togglePdf()">Show PDF</button>

<script>
const DOCS = {data_json};
const PAGE_SIZE = 75;

let currentDocIdx = -1;
let filteredRels = [];
let currentPage = 0;
let pdfVisible = true;
let currentPdfPath = null;

// ── Colour palette for predicates ─────────────────────────────────────────────
const PALETTES=[["#dbeafe","#1e40af"],["#dcfce7","#166534"],["#fef9c3","#854d0e"],
  ["#fce7f3","#9d174d"],["#ede9fe","#4c1d95"],["#ffedd5","#c2410c"],
  ["#cffafe","#164e63"],["#f0fdf4","#14532d"],["#fdf4ff","#701a75"]];
function predColour(p){{
  let h=0; for(let i=0;i<p.length;i++) h=(h*31+p.charCodeAt(i))&0xffff;
  return PALETTES[h%PALETTES.length];
}}

// ── Sidebar ───────────────────────────────────────────────────────────────────
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
      </div>
      <div class="doc-item-rels">${{doc.rels.length.toLocaleString()}} relationships</div>`;
    list.appendChild(el);
  }});
}}

function filterDocs(){{
  const q=document.getElementById("doc-search").value.toLowerCase();
  renderSidebar(q?DOCS.filter(d=>d.name.toLowerCase().includes(q)):DOCS);
}}

// ── Document selection ────────────────────────────────────────────────────────
function selectDoc(idx){{
  currentDocIdx=idx;
  const doc=DOCS[idx];

  // Left panel content
  document.getElementById("welcome").style.display="none";
  document.getElementById("doc-view").style.display="flex";

  document.getElementById("doc-title").textContent=doc.name;
  document.getElementById("doc-chips").innerHTML=`
    <span class="chip family">${{esc(doc.family)}}</span>
    ${{doc.pages?`<span class="chip pages">${{doc.pages}} page${{doc.pages!==1?"s":""}}</span>`:""}}
    <span class="chip size">${{fmtSize(doc.size)}}</span>`;

  renderSummary(doc);

  const rels=doc.rels;
  const preds=new Set(rels.map(r=>r.p));
  const ents=new Set([...rels.map(r=>r.s),...rels.map(r=>r.o)]);
  document.getElementById("stat-total").textContent=rels.length.toLocaleString();
  document.getElementById("stat-predicates").textContent=preds.size.toLocaleString();
  document.getElementById("stat-entities").textContent=ents.size.toLocaleString();

  const sel=document.getElementById("pred-filter");
  const sortedP=[...preds].sort((a,b)=>a.localeCompare(b));
  sel.innerHTML=`<option value="">All predicates (${{preds.size}})</option>`+
    sortedP.map(p=>`<option value="${{esc(p)}}">${{esc(p)}}</option>`).join("");
  document.getElementById("rel-search").value="";

  // PDF panel
  loadPdf(doc);

  // Sidebar active state
  document.querySelectorAll(".doc-item").forEach(el=>
    el.classList.toggle("active",parseInt(el.dataset.idx)===idx));

  applyFilter();
}}

// ── PDF panel ─────────────────────────────────────────────────────────────────
function loadPdf(doc){{
  currentPdfPath=doc.pdf;
  const frame=document.getElementById("pdf-frame");
  const placeholder=document.getElementById("pdf-placeholder");
  const toolbar=document.getElementById("pdf-title");
  const btnTab=document.getElementById("btn-new-tab");

  toolbar.textContent=doc.name;
  btnTab.style.display="";

  if(doc.family==="pdf"){{
    // Encode each path segment but preserve slashes
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
  const rp=document.getElementById("right-panel");
  const divider=document.getElementById("divider");
  const showBtn=document.getElementById("btn-show-pdf");
  rp.classList.toggle("hidden",!pdfVisible);
  divider.style.display=pdfVisible?"":"none";
  showBtn.classList.toggle("visible",!pdfVisible);
  document.getElementById("btn-hide-pdf").textContent=pdfVisible?"Hide PDF":"Hide PDF";
}}

// ── Summary rendering ─────────────────────────────────────────────────────────
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

// ── Relationships ─────────────────────────────────────────────────────────────
function applyFilter(){{
  const doc=DOCS[currentDocIdx];
  if(!doc) return;
  const pred=document.getElementById("pred-filter").value;
  const q=document.getElementById("rel-search").value.toLowerCase();
  filteredRels=doc.rels.filter(r=>{{
    if(pred&&r.p!==pred) return false;
    if(q&&!(r.s+" "+r.p+" "+r.o+" "+r.e).toLowerCase().includes(q)) return false;
    return true;
  }});
  currentPage=0;
  renderTable();
}}

function renderTable(){{
  const tbody=document.getElementById("rel-tbody");
  const table=document.getElementById("rel-table");
  const noRels=document.getElementById("no-rels");
  const total=filteredRels.length;
  document.getElementById("rel-count-label").textContent=
    total.toLocaleString()+" relationship"+(total!==1?"s":"");

  if(!total){{
    table.style.display="none"; noRels.style.display="";
    document.getElementById("pagination").style.display="none";
    return;
  }}
  noRels.style.display="none"; table.style.display="";

  const totalPages=Math.ceil(total/PAGE_SIZE);
  const rows=filteredRels.slice(currentPage*PAGE_SIZE,(currentPage+1)*PAGE_SIZE);
  tbody.innerHTML=rows.map(r=>{{
    const [bg,fg]=predColour(r.p);
    const pct=Math.round(r.c*100);
    return`<tr>
      <td><div class="entity-name">${{esc(r.s)}}</div>${{r.sk?`<div class="entity-kind">${{esc(r.sk)}}</div>`:""}}</td>
      <td><span class="pred-pill" style="background:${{bg}};color:${{fg}}">${{esc(r.p)}}</span></td>
      <td><div class="entity-name">${{esc(r.o)}}</div>${{r.ok?`<div class="entity-kind">${{esc(r.ok)}}</div>`:""}}</td>
      <td>${{r.e?`<div class="evidence-text" onclick="this.classList.toggle('expanded')">${{esc(r.e)}}</div>`:'<span style="color:#cbd5e1">—</span>'}}</td>
      <td class="page-cell">${{r.pg!=null?"p."+r.pg:"—"}}</td>
      <td><div class="conf-bar"><div class="conf-fill" style="width:${{pct}}%"></div></div><div class="conf-pct">${{pct}}%</div></td>
    </tr>`;
  }}).join("");

  const pag=document.getElementById("pagination");
  pag.style.display=totalPages>1?"flex":"none";
  document.getElementById("btn-prev").disabled=currentPage===0;
  document.getElementById("btn-next").disabled=currentPage>=totalPages-1;
  document.getElementById("page-info").textContent=`Page ${{currentPage+1}} of ${{totalPages}}`;
}}

function changePage(d){{
  currentPage=Math.max(0,Math.min(Math.ceil(filteredRels.length/PAGE_SIZE)-1,currentPage+d));
  renderTable();
  document.getElementById("rel-table-wrap").scrollIntoView({{behavior:"smooth",block:"nearest"}});
}}

// ── Draggable divider ─────────────────────────────────────────────────────────
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

// Init
renderSidebar(DOCS);
const first=DOCS.findIndex(d=>d.summary);
if(first>=0) selectDoc(first);
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build self-contained HTML document viewer")
    parser.add_argument("--workspace", type=Path, default=Path("./workspace"))
    args = parser.parse_args()

    print("Loading pipeline outputs…")
    docs = load_data(args.workspace)
    summarised = sum(1 for d in docs if d["summary"])
    total_rels = sum(len(d["rels"]) for d in docs)
    print(f"  {len(docs)} documents, {summarised} with summaries, {total_rels:,} relationships")

    html = generate_html(docs)
    out = args.workspace / "viewer.html"
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size // 1024
    print(f"  Written → {out}  ({size_kb} KB)")
    print(f"  Open in browser:  open {out}")


if __name__ == "__main__":
    main()
