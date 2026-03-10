from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Task 1: Config paths
# ---------------------------------------------------------------------------

from agentic_parse.config import Settings


def test_config_entity_names_paths(tmp_path: Path) -> None:
    s = Settings(workspace=tmp_path, raw_root=tmp_path / "raw")
    assert s.entity_names_dir == tmp_path / "outputs" / "entity_names"
    assert s.entity_registry_json == tmp_path / "outputs" / "entity_registry.json"
    assert s.entity_registry_jsonl == tmp_path / "outputs" / "entity_registry.jsonl"
    s.ensure_dirs()
    assert s.entity_names_dir.exists()
