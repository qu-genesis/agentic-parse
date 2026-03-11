import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from build_viewer_lite import load_data, generate_html


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


def test_generate_html_has_entity_card():
    html = generate_html([], {}, {}, [])
    assert 'id="entity-card"' in html
    assert 'id="entity-card-body"' in html
    assert "renderEntityCard" in html
    assert "toggleEntityCard" in html


def test_generate_html_entity_card_fallback_removes_raw_entities_when_covered():
    html = generate_html([], {}, {}, [])
    assert "hasRegistryEntries" in html
