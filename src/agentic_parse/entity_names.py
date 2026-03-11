from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from .chunk_embed import retrieve_top_k_chunks
from .config import Settings
from .db import Connection
from .llm import get_llm_client
from .telemetry import record_stage_metric
from .utils import atomic_write_text, write_json

ENTITY_NAMES_MAX_WORKERS = int(os.getenv("ENTITY_NAMES_MAX_WORKERS", "16"))
_RESOLUTION_BATCH_SIZE = 100
_CONTEXT_MAX_CHARS = 4000


def _build_extraction_prompt(*, summary_text: str, chunks_text: str) -> str:
    context = f"{summary_text}\n\n{chunks_text}".strip()
    if len(context) > _CONTEXT_MAX_CHARS:
        context = context[:_CONTEXT_MAX_CHARS]
    return (
        "Extract all named persons and organizations from the following document context.\n"
        'Return {"persons": ["Full Name", ...], "organizations": ["Org Name", ...]}\n'
        "Include every variant or abbreviation you see — do not normalize.\n"
        "If none found, return empty lists.\n\n"
        f"CONTEXT:\n{context}\n\n"
        "Return JSON only."
    )


def _parse_extraction_response(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {"persons": [], "organizations": []}
    persons = payload.get("persons")
    orgs = payload.get("organizations")
    return {
        "persons": [str(v).strip() for v in persons if str(v).strip()] if isinstance(persons, list) else [],
        "organizations": [str(v).strip() for v in orgs if str(v).strip()] if isinstance(orgs, list) else [],
    }
