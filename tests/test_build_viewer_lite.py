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
