from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    raw_root: Path

    @property
    def db_dsn(self) -> str:
        return os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/agentic_parse",
        )

    @property
    def catalogue_jsonl(self) -> Path:
        return self.workspace / "outputs" / "document_catalogue.jsonl"

    @property
    def relationships_jsonl(self) -> Path:
        return self.workspace / "outputs" / "relationships.jsonl"

    @property
    def fallback_events_jsonl(self) -> Path:
        return self.workspace / "outputs" / "fallback_events.jsonl"

    @property
    def stage_metrics_jsonl(self) -> Path:
        return self.workspace / "outputs" / "stage_metrics.jsonl"

    @property
    def vector_index_jsonl(self) -> Path:
        return self.workspace / "outputs" / "vector_index.jsonl"

    @property
    def paystubs_jsonl(self) -> Path:
        return self.workspace / "outputs" / "paystubs.jsonl"

    @property
    def costly_calls_jsonl(self) -> Path:
        return self.workspace / "outputs" / "costly_calls.jsonl"

    @property
    def entities_dir(self) -> Path:
        return self.workspace / "outputs" / "entities"

    @property
    def transcripts_dir(self) -> Path:
        return self.workspace / "derived" / "transcripts"

    @property
    def chunks_dir(self) -> Path:
        return self.workspace / "derived" / "chunks"

    @property
    def summaries_dir(self) -> Path:
        return self.workspace / "derived" / "summaries"

    @property
    def fallback_cache_dir(self) -> Path:
        return self.workspace / "derived" / "llm_fallback_cache"

    @property
    def llm_cache_dir(self) -> Path:
        return self.workspace / "derived" / "llm_cache"

    def ensure_dirs(self) -> None:
        dirs = [
            self.workspace / "state",
            self.workspace / "outputs",
            self.entities_dir,
            self.transcripts_dir,
            self.chunks_dir,
            self.summaries_dir,
            self.fallback_cache_dir,
            self.llm_cache_dir,
        ]
        for directory in dirs:
            directory.mkdir(parents=True, exist_ok=True)
