# Entity Registry Viewer Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the raw `key_entities` entity filter in `viewer_lite.html` with canonical, alias-resolved data from `entity_registry.json`, adding two separate People / Organizations dropdowns to the Catalogue page and a collapsible entity card to the Viewer page.

**Architecture:** Single file change to `scripts/build_viewer_lite.py`. `load_data()` reads the optional `entity_registry.json`; `generate_html()` embeds it as `ENTITY_REGISTRY` and generates updated HTML/CSS/JS. All registry-dependent UI degrades gracefully when the file is absent.

**Tech Stack:** Python, plain HTML/CSS/JS (no framework), existing `build_viewer_lite.py` pattern.

---

### Task 1: `load_data()` reads entity_registry.json

**Files:**
- Modify: `scripts/build_viewer_lite.py`
- Create: `tests/test_build_viewer_lite.py`

**Step 1: Write the failing test**

```python
# tests/test_build_viewer_lite.py
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from build_viewer_lite import load_data


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    out = ws / "outputs"
    out.mkdir(parents=True)
    (out / "document_catalogue.jsonl").write_text("")
    return ws


def test_load_data_returns_empty_registry_when_file_absent(tmp_path):
    ws = _make_workspace(tmp_path)
    _, _, _, registry = load_data(ws)
    assert registry == []


def test_load_data_returns_registry_when_file_present(tmp_path):
    ws = _make_workspace(tmp_path)
    entries = [
        {"entity_id": "ent_abc", "canonical_name": "john smith", "kind": "person",
         "aliases": ["John Smith"], "document_ids": ["doc_1"], "mention_count": 2},
    ]
    (ws / "outputs" / "entity_registry.json").write_text(json.dumps(entries))
    _, _, _, registry = load_data(ws)
    assert len(registry) == 1
    assert registry[0]["canonical_name"] == "john smith"


def test_load_data_returns_empty_registry_on_bad_json(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws / "outputs" / "entity_registry.json").write_text("not json")
    _, _, _, registry = load_data(ws)
    assert registry == []
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_build_viewer_lite.py -v
```

Expected: `TypeError: cannot unpack non-iterable` (load_data returns 3 values, not 4)

**Step 3: Update `load_data()` in `build_viewer_lite.py`**

Change the function signature and return value. Find the line:

```python
def load_data(workspace: Path) -> tuple[list[dict], dict, dict]:
```

Replace with:

```python
def load_data(workspace: Path) -> tuple[list[dict], dict, dict, list[dict]]:
```

Add registry loading just before the `return` at the end of `load_data()`:

```python
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
```

Also update the `return docs, grouped, entity_map` line that currently ends `load_data()` — replace it with the block above.

Update the two call sites in `main()`:

```python
    docs, grouped_catalogue, entity_map, registry = load_data(args.workspace)
```

and:

```python
    html = generate_html(docs, grouped_catalogue, entity_map, registry)
```

Update `generate_html` signature (just the def line for now — implementation in Task 2):

```python
def generate_html(docs: list[dict], grouped_catalogue: dict, entity_map: dict, registry: list[dict]) -> str:
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_build_viewer_lite.py -v
```

Expected: 3 PASS

**Step 5: Commit**

```bash
git add scripts/build_viewer_lite.py tests/test_build_viewer_lite.py
git commit -m "feat: load entity_registry.json in viewer build script"
```

---

### Task 2: Embed `ENTITY_REGISTRY` JS constant and precomputes

**Files:**
- Modify: `scripts/build_viewer_lite.py`
- Modify: `tests/test_build_viewer_lite.py`

**Step 1: Write the failing test**

Add to `tests/test_build_viewer_lite.py`:

```python
from build_viewer_lite import generate_html


def test_generate_html_embeds_entity_registry():
    registry = [
        {"entity_id": "ent_abc", "canonical_name": "john smith", "kind": "person",
         "aliases": ["John Smith"], "document_ids": ["doc_1"], "mention_count": 2},
        {"entity_id": "ent_def", "canonical_name": "acme corp", "kind": "organization",
         "aliases": ["Acme Corp"], "document_ids": ["doc_1"], "mention_count": 1},
    ]
    html = generate_html([], {}, {}, registry)
    assert "ENTITY_REGISTRY" in html
    assert "john smith" in html
    assert "acme corp" in html
    assert "REGISTRY_BY_DOC" in html
    assert "HAS_REGISTRY" in html
    assert "PEOPLE" in html
    assert "ORGS" in html


def test_generate_html_empty_registry_sets_has_registry_false():
    html = generate_html([], {}, {}, [])
    assert "ENTITY_REGISTRY = []" in html or "ENTITY_REGISTRY=[]" in html
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_build_viewer_lite.py::test_generate_html_embeds_entity_registry -v
```

Expected: FAIL — `ENTITY_REGISTRY` not in html

**Step 3: Add registry JS constants to `generate_html()`**

In `generate_html()`, add this line alongside the existing `data_json`, `grouped_json`, `entity_json` locals:

```python
    registry_json = json.dumps(registry, ensure_ascii=False, separators=(",", ":"))
```

In the `<script>` block of the HTML template, find the existing constants at the top:

```js
const DOCS = {data_json};
const CATALOGUE = {grouped_json};
const ENTITY_MAP = {entity_json};
const ENTITIES = Object.keys(ENTITY_MAP).sort((a,b)=>a.localeCompare(b));
const DOC_BY_ID = Object.fromEntries(DOCS.map(d => [d.id, d]));
```

Replace with (note: curly braces inside JS must be doubled `{{` `}}` in the Python f-string):

```python
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
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_build_viewer_lite.py -v
```

Expected: All PASS

**Step 5: Commit**

```bash
git add scripts/build_viewer_lite.py tests/test_build_viewer_lite.py
git commit -m "feat: embed ENTITY_REGISTRY and precomputes in viewer JS"
```

---

### Task 3: Catalogue page — two dropdowns with mutual exclusion

**Files:**
- Modify: `scripts/build_viewer_lite.py`
- Modify: `tests/test_build_viewer_lite.py`

**Step 1: Write the failing tests**

Add to `tests/test_build_viewer_lite.py`:

```python
def test_generate_html_has_two_registry_dropdowns():
    html = generate_html([], {}, {}, [])
    assert 'id="people-select"' in html
    assert 'id="orgs-select"' in html
    assert 'id="registry-filter-controls"' in html
    assert 'id="legacy-filter-controls"' in html


def test_generate_html_has_registry_js_functions():
    html = generate_html([], {}, {}, [])
    assert "onRegistryFilterChange" in html
    assert "openRegistryDoc" in html
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_build_viewer_lite.py -k "two_registry or registry_js" -v
```

Expected: FAIL

**Step 3: Replace catalogue filter HTML**

In the HTML template, find the existing filter controls block:

```html
    <div id="catalogue-filter-controls">
      <select id="entity-select" onchange="onEntityChange()">
        <option value="">All entities</option>
      </select>
      <button id="btn-entity-open" onclick="openEntityDoc()" disabled>→ Open</button>
    </div>
```

Replace with:

```html
    <div id="catalogue-filter-controls" style="display:flex;align-items:center;gap:14px">
      <div id="registry-filter-controls" style="display:flex;align-items:center;gap:12px">
        <div style="display:flex;align-items:center;gap:6px">
          <select id="people-select" onchange="onRegistryFilterChange('people')">
            <option value="">All people</option>
          </select>
          <button id="btn-people-open" class="entity-open-btn" onclick="openRegistryDoc('people')" disabled>→ Open</button>
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          <select id="orgs-select" onchange="onRegistryFilterChange('orgs')">
            <option value="">All organizations</option>
          </select>
          <button id="btn-orgs-open" class="entity-open-btn" onclick="openRegistryDoc('orgs')" disabled>→ Open</button>
        </div>
      </div>
      <div id="legacy-filter-controls" style="display:flex;align-items:center;gap:8px">
        <select id="entity-select" onchange="onEntityChange()">
          <option value="">All entities</option>
        </select>
        <button id="btn-entity-open" class="entity-open-btn" onclick="openEntityDoc()" disabled>→ Open</button>
      </div>
    </div>
```

**Step 4: Update CSS**

In the `<style>` block, find the existing `#entity-select` and `#btn-entity-open` rules and replace/extend them:

```css
.entity-select-ctrl{{
  padding:5px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--card-bg);color:var(--text);font-size:12.5px;
  max-width:220px;cursor:pointer;outline:none;
}}
.entity-select-ctrl:focus{{border-color:var(--accent)}}
#entity-select{{
  padding:5px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--card-bg);color:var(--text);font-size:12.5px;
  max-width:220px;cursor:pointer;outline:none;
}}
#entity-select:focus{{border-color:var(--accent)}}
#people-select,#orgs-select{{
  padding:5px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--card-bg);color:var(--text);font-size:12.5px;
  max-width:200px;cursor:pointer;outline:none;
}}
#people-select:focus,#orgs-select:focus{{border-color:var(--accent)}}
.entity-open-btn{{
  padding:5px 12px;border-radius:6px;border:none;background:var(--accent);
  color:#fff;font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap;
}}
.entity-open-btn:hover{{background:#4338ca}}
.entity-open-btn:disabled{{background:#c7d2fe;cursor:default}}
```

Remove the old `#btn-entity-open` CSS block if it exists separately.

**Step 5: Replace JS catalogue functions**

Find the existing `initCataloguePage`, `onEntityChange`, `openEntityDoc`, and `renderCataloguePage` functions and replace them all with:

```js
function initCataloguePage() {{
  if (HAS_REGISTRY) {{
    document.getElementById('legacy-filter-controls').style.display='none';
    const pSel=document.getElementById('people-select');
    PEOPLE.forEach(e=>{{
      const opt=document.createElement('option');
      opt.value=e.entity_id;
      opt.textContent=e.canonical_name;
      opt.title=e.mention_count+' mention'+(e.mention_count!==1?'s':'');
      pSel.appendChild(opt);
    }});
    const oSel=document.getElementById('orgs-select');
    ORGS.forEach(e=>{{
      const opt=document.createElement('option');
      opt.value=e.entity_id;
      opt.textContent=e.canonical_name;
      opt.title=e.mention_count+' mention'+(e.mention_count!==1?'s':'');
      oSel.appendChild(opt);
    }});
  }} else {{
    document.getElementById('registry-filter-controls').style.display='none';
    const sel=document.getElementById('entity-select');
    ENTITIES.forEach(e=>{{
      const opt=document.createElement('option');
      opt.value=e; opt.textContent=e;
      sel.appendChild(opt);
    }});
  }}
  renderCataloguePage(null);
}}

function onRegistryFilterChange(kind) {{
  const otherId=kind==='people'?'orgs-select':'people-select';
  const otherBtnId=kind==='people'?'btn-orgs-open':'btn-people-open';
  document.getElementById(otherId).value='';
  document.getElementById(otherBtnId).disabled=true;
  const selId=kind==='people'?'people-select':'orgs-select';
  const btnId=kind==='people'?'btn-people-open':'btn-orgs-open';
  const entityId=document.getElementById(selId).value;
  document.getElementById(btnId).disabled=!entityId;
  if(entityId){{
    const entry=ENTITY_REGISTRY.find(e=>e.entity_id===entityId);
    renderCataloguePage(entry?new Set(entry.document_ids):null);
  }} else {{
    renderCataloguePage(null);
  }}
}}

function openRegistryDoc(kind) {{
  const selId=kind==='people'?'people-select':'orgs-select';
  const entityId=document.getElementById(selId).value;
  if(!entityId) return;
  const entry=ENTITY_REGISTRY.find(e=>e.entity_id===entityId);
  if(!entry||!entry.document_ids.length) return;
  const idx=DOCS.findIndex(d=>d.id===entry.document_ids[0]);
  if(idx>=0){{showPage('viewer');selectDoc(idx);}}
}}

function onEntityChange() {{
  const entity=document.getElementById('entity-select').value;
  document.getElementById('btn-entity-open').disabled=!entity;
  renderCataloguePage(entity?new Set(ENTITY_MAP[entity]||[]):null);
}}

function openEntityDoc() {{
  const entity=document.getElementById('entity-select').value;
  if(!entity) return;
  const docIds=ENTITY_MAP[entity]||[];
  if(!docIds.length) return;
  const idx=DOCS.findIndex(d=>d.id===docIds[0]);
  if(idx>=0){{showPage('viewer');selectDoc(idx);}}
}}

function renderCataloguePage(matchingIds) {{
  const listEl=document.getElementById('catalogue-page-list');
  const emptyEl=document.getElementById('catalogue-page-empty');
  const groups=(CATALOGUE&&Array.isArray(CATALOGUE.groups))?CATALOGUE.groups:[];
  if(!groups.length){{listEl.innerHTML='';emptyEl.style.display='';return;}}
  emptyEl.style.display='none';
  listEl.innerHTML=groups.map(group=>{{
    const groupDocIds=Array.isArray(group.document_ids)?group.document_ids:[];
    const hasMatch=!matchingIds||groupDocIds.some(id=>matchingIds.has(id));
    const dimmed=!!(matchingIds&&!hasMatch);
    return catalogueGroupHtml(group,null,matchingIds,dimmed);
  }}).join('');
}}
```

**Step 6: Run tests to verify they pass**

```bash
pytest tests/test_build_viewer_lite.py -v
```

Expected: All PASS

**Step 7: Commit**

```bash
git add scripts/build_viewer_lite.py tests/test_build_viewer_lite.py
git commit -m "feat: add two-dropdown registry filter to catalogue page"
```

---

### Task 4: Per-doc entity card with collapse/expand

**Files:**
- Modify: `scripts/build_viewer_lite.py`
- Modify: `tests/test_build_viewer_lite.py`

**Step 1: Write the failing tests**

Add to `tests/test_build_viewer_lite.py`:

```python
def test_generate_html_has_entity_card():
    html = generate_html([], {}, {}, [])
    assert 'id="entity-card"' in html
    assert 'id="entity-card-body"' in html
    assert "renderEntityCard" in html
    assert "toggleEntityCard" in html


def test_generate_html_entity_card_fallback_removes_raw_entities_when_covered():
    # buildSummaryHtml should suppress key_entities when registry covers the doc
    html = generate_html([], {}, {}, [])
    assert "hasRegistryEntries" in html
```

**Step 2: Run to verify failure**

```bash
pytest tests/test_build_viewer_lite.py -k "entity_card" -v
```

Expected: FAIL

**Step 3: Add entity card CSS**

In the `<style>` block, add after the existing `.cat-doc-btn.entity-match` rule:

```css
/* ── Entity card ── */
#entity-card{{display:none}}
.ent-section{{margin-bottom:10px}}
.ent-section:last-child{{margin-bottom:0}}
.ent-kind-label{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#475569;margin-bottom:6px}}
.ent-chips{{display:flex;flex-wrap:wrap;gap:6px}}
.ent-chip{{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:500;border:1px solid}}
.ent-chip.person{{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe}}
.ent-chip.org{{background:#f0fdf4;color:#15803d;border-color:#bbf7d0}}
#entity-card-toggle{{
  font-size:11px;padding:2px 8px;border-radius:5px;border:1px solid var(--border);
  background:var(--bg);color:var(--muted);cursor:pointer;
}}
#entity-card-toggle:hover{{border-color:var(--accent);color:var(--accent)}}
```

**Step 4: Add entity card HTML**

In the viewer page, find the doc-content area:

```html
        <div class="card">
          <div class="card-header"><span class="card-title">Summary</span></div>
          <div class="card-body">
            <div id="summary-structured" style="display:none"></div>
            <div id="summary-text" style="display:none"></div>
            <div id="no-summary" style="display:none">No summary generated for this document.</div>
          </div>
        </div>
```

Add the entity card immediately after the closing `</div>` of the Summary card:

```html
        <div class="card" id="entity-card">
          <div class="card-header">
            <span class="card-title">People &amp; Organizations</span>
            <button id="entity-card-toggle" onclick="toggleEntityCard()">Show all</button>
          </div>
          <div class="card-body" id="entity-card-body"></div>
        </div>
```

**Step 5: Add entity card JS**

Add a new JS section after the `renderSummary` function:

```js
// ── Entity card ────────────────────────────────────────────────────────────────
let entityCardExpanded=false;
const ENTITY_CARD_MAX=3;

function renderEntityCard(doc){{
  const card=document.getElementById('entity-card');
  const body=document.getElementById('entity-card-body');
  const toggle=document.getElementById('entity-card-toggle');
  const entries=REGISTRY_BY_DOC[doc.id]||[];
  if(!entries.length){{card.style.display='none';return;}}
  card.style.display='';
  entityCardExpanded=false;
  toggle.textContent='Show all';
  _renderEntityCardBody(body,entries,false);
}}

function _renderEntityCardBody(body,entries,expanded){{
  const people=entries.filter(e=>e.kind==='person');
  const orgs=entries.filter(e=>e.kind==='organization');
  const chips=(list,cls)=>list.map(e=>`<span class="ent-chip ${{cls}}">${{esc(e.canonical_name)}}</span>`).join('');
  let h='';
  if(people.length){{
    const shown=expanded?people:people.slice(0,ENTITY_CARD_MAX);
    h+=`<div class="ent-section"><div class="ent-kind-label">People</div><div class="ent-chips">${{chips(shown,'person')}}</div></div>`;
  }}
  if(orgs.length){{
    const shown=expanded?orgs:orgs.slice(0,ENTITY_CARD_MAX);
    h+=`<div class="ent-section"><div class="ent-kind-label">Organizations</div><div class="ent-chips">${{chips(shown,'org')}}</div></div>`;
  }}
  body.innerHTML=h;
  const toggle=document.getElementById('entity-card-toggle');
  toggle.style.display=(people.length>ENTITY_CARD_MAX||orgs.length>ENTITY_CARD_MAX)?'':'none';
}}

function toggleEntityCard(){{
  entityCardExpanded=!entityCardExpanded;
  const doc=DOCS[currentDocIdx];
  if(!doc) return;
  const entries=REGISTRY_BY_DOC[doc.id]||[];
  document.getElementById('entity-card-toggle').textContent=entityCardExpanded?'Show less':'Show all';
  _renderEntityCardBody(document.getElementById('entity-card-body'),entries,entityCardExpanded);
}}
```

**Step 6: Wire `renderEntityCard` into `selectDoc`**

Find `renderSummary(doc);` inside `selectDoc()` and add the entity card call immediately after:

```js
  renderSummary(doc);
  renderEntityCard(doc);
```

**Step 7: Suppress raw entities in summary when registry covers the doc**

In `buildSummaryHtml`, change its signature and suppress the raw entities section when registry data exists. Find:

```js
function buildSummaryHtml(s){{
```

Replace with:

```js
function buildSummaryHtml(s,docId){{
```

And find this line inside the function:

```js
  if(entities.length) h+=sumList("Entities",entities);
```

Replace with:

```js
  const hasRegistryEntries=HAS_REGISTRY&&(REGISTRY_BY_DOC[docId]||[]).length>0;
  if(!hasRegistryEntries&&entities.length) h+=sumList("Entities",entities);
```

Find the call site of `buildSummaryHtml` (inside `renderSummary`):

```js
    structured.innerHTML=buildSummaryHtml(doc.summary_json);
```

Replace with:

```js
    structured.innerHTML=buildSummaryHtml(doc.summary_json,doc.id);
```

**Step 8: Run full test suite**

```bash
pytest tests/test_build_viewer_lite.py -v
```

Expected: All PASS

**Step 9: Smoke-test the build**

```bash
uv run python scripts/build_viewer_lite.py --workspace ./workspace
```

Expected: `Written → workspace/viewer_lite.html` with no errors. Open in browser and verify:
- Catalogue page shows People / Organizations dropdowns (or legacy dropdown if registry absent)
- Selecting a person/org highlights matching doc buttons
- Viewer page shows "People & Organizations" card for docs with registry entries, collapsed to 3 chips per kind with "Show all" toggle

**Step 10: Commit**

```bash
git add scripts/build_viewer_lite.py tests/test_build_viewer_lite.py
git commit -m "feat: add collapsible entity card to viewer doc panel"
```
