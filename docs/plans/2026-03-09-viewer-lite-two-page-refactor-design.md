# Design: Viewer Lite Two-Page Refactor

**Date:** 2026-03-09
**Branch:** document-catalogue-panel
**File:** `scripts/build_viewer_lite.py`

## Objective

Refactor `build_viewer_lite.py` to produce a two-page HTML viewer:
1. **Page 1 — Catalogue** (landing): document groups/subgroups on prominent display, entity filter tucked top-right.
2. **Page 2 — Viewer**: the existing split-panel (sidebar + summary + PDF), unchanged.

## Layout

### Top Nav Bar
- Same dark background as current sidebar (`--sidebar-bg`).
- App title ("Document Viewer") on the left.
- Two tab buttons on the right: **Catalogue** | **Viewer**, accent-highlighted on active.
- Clicking a tab shows/hides the corresponding page div; no URL routing needed.

### Page 1 — Catalogue

```
┌──────────────────────────────────────────────────────────┐
│  Document Catalogue              [Filter by entity ▼] [→ Open] │
├──────────────────────────────────────────────────────────┤
│  ┌─ legal documents ──────────────────────────────────┐  │
│  │  Description text                                  │  │
│  │  Acme Corp: [doc-a.pdf] [doc-b.pdf]                │  │
│  │  Documents:  [doc-c.pdf]                           │  │
│  └────────────────────────────────────────────────────┘  │
│  ┌─ invoices ─────────────────────────────────────────┐  │
│  │  ...                                               │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

- Groups/subgroups are the primary visual element — full-width cards, same style as current `cat-group`.
- Entity filter is a `<select>` dropdown in the top-right of the catalogue header. Populated from all `key_entities` across all `summary_json` objects (deduped, sorted).
- "→ Open" button: navigates to the Viewer tab and auto-selects the first document that mentions the chosen entity.
- When an entity is selected, group cards containing no matching documents are dimmed (opacity reduced); matching doc pills are highlighted.
- "All entities" option resets the filter.

### Page 2 — Viewer

Exactly the current layout: sidebar with search + doc list on the left, resizable split panel with summary on the left and PDF on the right. No changes to functionality.

- Clicking a doc pill on the Catalogue page navigates to the Viewer tab with that document selected.

## Data Changes (Python)

- Extract and collect all `key_entities` from each doc's `summary_json` during `load_data`.
- Embed the entity→document_id mapping as a JS constant `ENTITY_MAP` in the generated HTML.

## Entity Extraction Logic

- Source: `doc["summary_json"]["key_entities"]` — a list of strings per document.
- Collect all unique entity strings across all docs, sort alphabetically.
- Build a map: `{ entity_string: [doc_id, ...] }` for filtering and navigation.

## What is NOT changing

- The `load_data` function signature and output structure (adding only `entities` field per doc).
- All existing CSS variables and component styles.
- The PDF viewer, drag divider, and summary rendering logic.
