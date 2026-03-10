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


# ---------------------------------------------------------------------------
# Task 2: DB column
# ---------------------------------------------------------------------------

from agentic_parse.db import init_schema, connect


def test_db_has_status_entity_names_column(tmp_path: Path) -> None:
    import os
    dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/agentic_parse")
    conn = connect(dsn)
    init_schema(conn)
    # Should not raise; column must exist after init
    conn.execute("SELECT status_entity_names FROM documents LIMIT 0")
    conn.close()


# ---------------------------------------------------------------------------
# Task 3: Pure extraction helpers
# ---------------------------------------------------------------------------

from agentic_parse.entity_names import _build_extraction_prompt, _parse_extraction_response


def test_build_extraction_prompt_includes_summary_and_chunks() -> None:
    prompt = _build_extraction_prompt(
        summary_text="John Smith works at Safari Restaurant LLC.",
        chunks_text="Mary O'Brien is the manager.",
    )
    assert "John Smith" in prompt
    assert "Mary O'Brien" in prompt
    assert "persons" in prompt
    assert "organizations" in prompt


def test_parse_extraction_response_valid() -> None:
    raw = {"persons": ["John Smith", "J. Smith"], "organizations": ["Safari Restaurant LLC"]}
    result = _parse_extraction_response(raw)
    assert result["persons"] == ["John Smith", "J. Smith"]
    assert result["organizations"] == ["Safari Restaurant LLC"]


def test_parse_extraction_response_tolerates_missing_keys() -> None:
    result = _parse_extraction_response({"persons": ["Alice"]})
    assert result["organizations"] == []


def test_parse_extraction_response_returns_empty_on_none() -> None:
    result = _parse_extraction_response(None)
    assert result == {"persons": [], "organizations": []}
