# Entity Registry Viewer Integration — Design

**Date:** 2026-03-10
**Status:** Approved

## Goal

Replace the current raw `key_entities` entity filter in `viewer_lite.html` with the canonical, alias-resolved `entity_registry.json` produced by the `entity-names` pipeline stage. Integrate registry data in two places: the catalogue filter controls and the per-doc summary panel.

---

## Current State

- **Catalogue filter:** single `<select>` populated from `key_entities` strings aggregated across all doc summaries. No normalization — "John Smith" and "J. Smith" are separate entries.
- **Per-doc summary card:** raw `key_entities` list rendered as a bullet list inside the structured summary.
- **Data source:** `entity_map: dict[str, list[str]]` built at viewer build time by scanning `summary_json.key_entities`.

---

## Design

### Data Flow (build time — `build_viewer_lite.py`)

1. `load_data()` attempts to read `outputs/entity_registry.json`.
2. If present, parses the registry array and returns it alongside existing data.
3. If absent, returns `[]` — all registry-dependent UI degrades gracefully.
4. `generate_html()` embeds the registry as a JS constant `ENTITY_REGISTRY`.

### JS Data Structures (runtime)

```js
// Full registry ([] if pipeline hasn't run entity-names)
const ENTITY_REGISTRY = [...];

// Filtered by kind, sorted by mention_count desc then canonical_name asc
const PEOPLE = ENTITY_REGISTRY.filter(e => e.kind === 'person');
const ORGS   = ENTITY_REGISTRY.filter(e => e.kind === 'organization');

// Fast per-doc lookup: doc_id → [entity entries]
const REGISTRY_BY_DOC = {};  // built at init from ENTITY_REGISTRY

// Legacy fallback (kept for docs with no registry coverage)
const ENTITY_MAP = {...};
```

### Catalogue Page — Two Dropdowns

Replace the single `#entity-select` + `#btn-entity-open` with:

```
[People ▼]  [→ Open]   [Organizations ▼]  [→ Open]
```

- `#people-select` / `#orgs-select`: populated from `PEOPLE` / `ORGS`, each option shows `canonical_name` (with mention count as option title tooltip).
- Selecting in one clears the other (mutual exclusion).
- Catalogue re-renders on change, highlighting docs whose `document_ids` include any matching entity.
- "→ Open" jumps to the first doc in the selected entity's `document_ids`.
- Both controls hidden (display:none) when `ENTITY_REGISTRY` is empty — falls back to existing single dropdown from `ENTITY_MAP`.

### Per-Doc Summary Panel — Collapsible Registry Card

A new "People & Organizations" card is added **below** the Summary card in the Viewer page, built from `REGISTRY_BY_DOC[doc.id]`.

- Default collapsed state: shows up to 3 people + 3 orgs as pill chips.
- "Show all" toggle reveals the full list.
- Each chip shows the `canonical_name`.
- The raw "Entities" bullet list in `buildSummaryHtml` is **removed** when the doc has registry coverage. Kept as fallback when `REGISTRY_BY_DOC[doc.id]` is empty or absent.

### Graceful Fallback

| Condition | Behaviour |
|-----------|-----------|
| `entity_registry.json` missing | Old single-dropdown + raw `key_entities` in summary |
| Doc has no registry entries | Raw `key_entities` shown in summary; doc not highlighted by either dropdown |
| Doc partially covered | Registry card shown with what's available |

---

## Files Changed

| File | Change |
|------|--------|
| `scripts/build_viewer_lite.py` | `load_data()` reads registry; `generate_html()` embeds `ENTITY_REGISTRY`; CSS + HTML for new controls and card; JS for two dropdowns, `REGISTRY_BY_DOC`, collapsible card |

No other files change — this is purely a viewer build script update.

---

## Non-Goals

- No clickable chip cross-navigation between Catalogue and Viewer pages.
- No alias display in the chips (canonical name only).
- No changes to pipeline code — registry is consumed read-only.
