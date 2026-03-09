# Viewer Lite Two-Page Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor `scripts/build_viewer_lite.py` to produce a two-page HTML viewer with a Catalogue landing page (group/subgroup display + entity filter) and the existing Document Viewer as page 2, connected by a top nav tab bar.

**Architecture:** A single Python file generates a self-contained HTML file. We add an `ENTITY_MAP` JS constant built from `key_entities` in each doc's `summary_json`. The HTML gains a top nav bar and two `<div>` pages toggled by JS — Page 1 is the full-width catalogue, Page 2 is the unchanged sidebar+split-panel viewer.

**Tech Stack:** Python 3, vanilla JS/HTML/CSS, no external dependencies (self-contained output).

---

### Task 1: Extract entities in `load_data` and embed `ENTITY_MAP` in JS

**Files:**
- Modify: `scripts/build_viewer_lite.py`

**Context:**
Each doc already has `summary_json` which may contain a `key_entities` list of strings. We need to collect all unique entities and build a map of `entity → [doc_id, ...]` to pass into the generated HTML.

**Step 1: Add entity extraction to `load_data`**

In `load_data`, after building `docs`, add:

```python
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

return docs, grouped, entity_map
```

Update the return type annotation and call sites. The function signature becomes:
```python
def load_data(workspace: Path) -> tuple[list[dict], dict, dict]:
```

**Step 2: Update `main()` to unpack the new return value**

```python
docs, grouped_catalogue, entity_map = load_data(args.workspace)
```

And pass `entity_map` to `generate_html`:
```python
html = generate_html(docs, grouped_catalogue, entity_map)
```

**Step 3: Update `generate_html` signature**

```python
def generate_html(docs: list[dict], grouped_catalogue: dict, entity_map: dict) -> str:
```

Add a new JS constant in the template (alongside `DOCS` and `CATALOGUE`):
```python
entity_json = json.dumps(
    {k: v for k, v in sorted(entity_map.items())},
    ensure_ascii=False, separators=(",", ":")
)
```

And in the HTML `<script>` block add:
```js
const ENTITY_MAP = {entity_json};
const ENTITIES = Object.keys(ENTITY_MAP).sort((a,b)=>a.localeCompare(b));
```

**Step 4: Run the script to verify it still produces valid output**

```bash
cd /Users/qugenes/Desktop/projects/agentic-parse
uv run python scripts/build_viewer_lite.py --workspace ./workspace
```

Expected: exits without error, prints document/summary/group counts, writes `workspace/viewer_lite.html`.

**Step 5: Commit**

```bash
git add scripts/build_viewer_lite.py
git commit -m "feat: extract key_entities into ENTITY_MAP for viewer"
```

---

### Task 2: Add top nav bar (HTML + CSS)

**Files:**
- Modify: `scripts/build_viewer_lite.py` (the HTML template inside `generate_html`)

**Context:**
We need a slim top nav bar that sits above everything, with the app title on the left and two tab buttons ("Catalogue", "Viewer") on the right. The active tab uses the accent color. The body layout changes from `display:flex` (horizontal) to `display:flex;flex-direction:column` to accommodate the nav bar above the content.

**Step 1: Add CSS for the nav bar**

Add inside the `<style>` block (before the closing `</style>`):

```css
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
```

**Step 2: Change `body` layout to vertical**

Replace:
```css
body{{font-family:var(--font);background:var(--bg);color:var(--text);height:100vh;display:flex;overflow:hidden;user-select:none}}
```
With:
```css
body{{font-family:var(--font);background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden;user-select:none}}
```

**Step 3: Add the nav bar HTML**

Insert at the top of `<body>`, before `<!-- Sidebar -->`:

```html
<!-- Top nav -->
<div id="top-nav">
  <span id="nav-title">Document Viewer</span>
  <div id="nav-tabs">
    <button class="nav-tab active" id="tab-catalogue" onclick="showPage('catalogue')">Catalogue</button>
    <button class="nav-tab" id="tab-viewer" onclick="showPage('viewer')">Viewer</button>
  </div>
</div>
```

**Step 4: Wrap existing sidebar+content-area in a `#viewer-page` div**

Wrap the existing `<!-- Sidebar -->` and `<!-- Content area -->` blocks (and the `#btn-show-pdf` button) in:
```html
<!-- Page 2: Viewer -->
<div id="viewer-page" style="display:none;flex:1;display:none;overflow:hidden;flex-direction:row">
  ... existing sidebar + content-area HTML ...
</div>
```

Note: use `flex:1;min-height:0` so it fills the remaining space below the nav bar.

**Step 5: Add JS tab switching function**

In the `<script>` block, add:

```js
function showPage(page) {{
  document.getElementById('catalogue-page').style.display = page==='catalogue' ? 'flex' : 'none';
  document.getElementById('viewer-page').style.display = page==='viewer' ? 'flex' : 'none';
  document.getElementById('tab-catalogue').classList.toggle('active', page==='catalogue');
  document.getElementById('tab-viewer').classList.toggle('active', page==='viewer');
}}
```

**Step 6: Run and visually verify**

```bash
uv run python scripts/build_viewer_lite.py --workspace ./workspace && open workspace/viewer_lite.html
```

Expected: top nav visible with "Catalogue" and "Viewer" tabs; switching works (catalogue page placeholder can be empty for now).

**Step 7: Commit**

```bash
git add scripts/build_viewer_lite.py
git commit -m "feat: add top nav tab bar to viewer-lite"
```

---

### Task 3: Build the Catalogue page

**Files:**
- Modify: `scripts/build_viewer_lite.py`

**Context:**
The catalogue page is a full-width scrollable page. Its header has the title "Document Catalogue" on the left and the entity filter controls (dropdown + "→ Open" button) on the right. Below are the group cards (already styled as `.cat-group`), but now rendered as the primary content instead of a card within a doc view.

**Step 1: Add CSS for catalogue page layout**

Add to `<style>`:

```css
/* ── Catalogue page ── */
#catalogue-page{{
  flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0;
}}
#catalogue-page-header{{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 24px;background:var(--card-bg);border-bottom:1px solid var(--border);
  flex-shrink:0;gap:12px;
}}
#catalogue-page-title{{font-size:16px;font-weight:700;color:var(--text)}}
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
```

**Step 2: Add the catalogue page HTML**

Insert before `<!-- Page 2: Viewer -->`:

```html
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
```

**Step 3: Add JS to populate entity dropdown on init**

In the `<script>` block, add an `initCataloguePage()` function called during page init:

```js
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
```

**Step 4: Add `renderCataloguePage(filterEntity)` function**

```js
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
    const dimmed = matchingIds && !hasMatch ? ' dimmed' : '';
    return `<div class="cat-group${{dimmed}}">${{catalogueGroupHtml(group, null, matchingIds)}}</div>`;
  }}).join('');
}}
```

**Step 5: Update `catalogueGroupHtml` to accept `matchingIds` and highlight matching pills**

The existing `catalogueGroupHtml(group, activeDocId)` is used in the viewer too. Add an optional third param:

```js
function catalogueGroupHtml(group, activeDocId, matchingIds=null) {{
  // ... existing logic unchanged, but pass matchingIds to catalogueDocButtons
}}

function catalogueDocButtons(docIds, activeDocId, matchingIds=null) {{
  return docIds.map(docId => {{
    const doc = DOC_BY_ID[docId];
    if (!doc) return '';
    const activeClass = docId === activeDocId ? ' active' : '';
    const matchClass = matchingIds && matchingIds.has(docId) ? ' entity-match' : '';
    return `<button class="cat-doc-btn${{activeClass}}${{matchClass}}" onclick="selectDocById('${{escJs(docId)}}')">${{esc(doc.name)}}</button>`;
  }}).join('');
}}
```

**Step 6: Add `onEntityChange()` and `openEntityDoc()` functions**

```js
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
```

**Step 7: Update `selectDocById` to switch to viewer page**

```js
function selectDocById(docId) {{
  const idx = DOCS.findIndex(d => d.id === docId);
  if (idx >= 0) {{
    showPage('viewer');
    selectDoc(idx);
  }}
}}
```

**Step 8: Update init block at the bottom of `<script>`**

Replace:
```js
renderSidebar(DOCS);
const first = DOCS.findIndex(d => d.summary);
if (first >= 0) selectDoc(first);
else if (DOCS.length) selectDoc(0);
```

With:
```js
initCataloguePage();
renderSidebar(DOCS);
```

(No auto-select on load — user starts on Catalogue page.)

**Step 9: Run and verify**

```bash
uv run python scripts/build_viewer_lite.py --workspace ./workspace && open workspace/viewer_lite.html
```

Expected:
- Opens to Catalogue page showing all groups/subgroups.
- Entity dropdown populated with all unique entities.
- Selecting entity dims non-matching groups, highlights matching doc pills.
- "→ Open" navigates to Viewer tab with first matching doc selected.
- Clicking a doc pill in catalogue navigates to Viewer tab with that doc selected.
- Viewer tab works exactly as before.

**Step 10: Commit**

```bash
git add scripts/build_viewer_lite.py
git commit -m "feat: add catalogue landing page with entity filter to viewer-lite"
```

---

### Task 4: Polish — remove old `#catalogue-list` card from doc view

**Files:**
- Modify: `scripts/build_viewer_lite.py`

**Context:**
The old viewer had a "Document Catalogue" card at the bottom of the doc detail panel (inside `#doc-content`). Now that the catalogue is its own page, this card is redundant and should be removed to keep the doc view clean.

**Step 1: Remove the catalogue card from `#doc-content`**

In the HTML template, remove this block:

```html
<div class="card">
  <div class="card-header"><span class="card-title">Document Catalogue</span></div>
  <div class="card-body">
    <div id="catalogue-empty" style="display:none">No grouped catalogue generated yet. Run the summarize stage to build it.</div>
    <div id="catalogue-list" style="display:none"></div>
  </div>
</div>
```

**Step 2: Remove the old `renderCatalogue(activeDocId)` call**

In `selectDoc`, remove the line:
```js
renderCatalogue(doc.id);
```

**Step 3: Remove the old `renderCatalogue` function**

Delete the functions `renderCatalogue` and `catalogueGroupHtml` (the old single-arg version). The new `catalogueGroupHtml(group, activeDocId, matchingIds)` in Task 3 replaces it.

Also remove CSS for `#catalogue-empty` and `#catalogue-list` if they are no longer referenced.

**Step 4: Run and verify**

```bash
uv run python scripts/build_viewer_lite.py --workspace ./workspace && open workspace/viewer_lite.html
```

Expected: Viewer tab shows only Summary card. No catalogue card in doc view. Catalogue page unaffected.

**Step 5: Commit**

```bash
git add scripts/build_viewer_lite.py
git commit -m "chore: remove redundant catalogue card from doc viewer panel"
```
