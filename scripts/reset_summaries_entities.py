"""Reset summaries, entities, relationships, and related logs, then rerun those stages."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agentic_parse.config import Settings
from agentic_parse.db import connect, init_schema
from agentic_parse.entities import extract_entities
from agentic_parse.summarize import summarize

WORKSPACE = Path(os.getenv("WORKSPACE", "./workspace")).resolve()
RAW_ROOT = Path(os.getenv("RAW_ROOT", "./raw")).resolve()

settings = Settings(workspace=WORKSPACE, raw_root=RAW_ROOT)
conn = connect(settings.db_dsn)
init_schema(conn)

print("=== Resetting summaries, entities, relationships, and logs ===")

# ── 1. DB: delete relationships, entity logs, summary/entity status resets ───
print("Deleting relationships from DB...")
conn.execute("DELETE FROM relationships")

print("Deleting costly_calls for summarize/entities stages...")
conn.execute("DELETE FROM costly_calls WHERE stage IN ('summarize', 'entities')")

print("Deleting stage_metrics for summarize/entities stages...")
conn.execute("DELETE FROM stage_metrics WHERE stage IN ('summarize', 'entities')")

print("Resetting summary_status and status_entities on all documents...")
conn.execute(
    "UPDATE documents SET summary_status = 'pending', status_entities = 'pending', updated_at = CURRENT_TIMESTAMP"
)

conn.commit()
print("DB reset done.")

# ── 2. Files: clear relationships.jsonl ───────────────────────────────────────
print("Clearing relationships.jsonl...")
settings.relationships_jsonl.write_text("", encoding="utf-8")

# ── 3. Files: filter costly_calls.jsonl to remove summarize/entities entries ─
print("Filtering costly_calls.jsonl...")
cc_path = settings.costly_calls_jsonl
if cc_path.exists():
    kept = []
    for line in cc_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("stage") not in ("summarize", "entities"):
                kept.append(line)
        except json.JSONDecodeError:
            kept.append(line)
    cc_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print(f"  kept {len(kept)} costly_calls entries")

# ── 4. Files: filter stage_metrics.jsonl ─────────────────────────────────────
print("Filtering stage_metrics.jsonl...")
sm_path = settings.stage_metrics_jsonl
if sm_path.exists():
    kept = []
    for line in sm_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("stage") not in ("summarize", "entities"):
                kept.append(line)
        except json.JSONDecodeError:
            kept.append(line)
    sm_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print(f"  kept {len(kept)} stage_metrics entries")

# ── 5. Files: delete entity JSON cards ───────────────────────────────────────
print("Deleting entity cards...")
entity_count = 0
if settings.entities_dir.exists():
    for f in settings.entities_dir.glob("*.json"):
        f.unlink()
        entity_count += 1
print(f"  deleted {entity_count} entity files")

# ── 6. Files: delete summary files ───────────────────────────────────────────
print("Deleting summary files...")
summary_count = 0
if settings.summaries_dir.exists():
    for f in settings.summaries_dir.rglob("*"):
        if f.is_file():
            f.unlink()
            summary_count += 1
print(f"  deleted {summary_count} summary files")

print("\n=== Rerunning summarize ===")
summarized = summarize(settings, conn)
print(f"summarized={summarized}")

print("\n=== Rerunning entities ===")
entities, rels = extract_entities(settings, conn)
print(f"entities_updated={entities} relationships_added={rels}")

print("\nDone.")
