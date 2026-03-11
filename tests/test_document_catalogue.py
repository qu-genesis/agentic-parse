from __future__ import annotations

import json
from pathlib import Path

from agentic_parse.document_catalogue import build_document_catalogue


class StubLLM:
    def __init__(self, responses: list[dict | None], enabled: bool = True) -> None:
        self.responses = list(responses)
        self.enabled = enabled
        self.model = "stub-model"
        self.tasks: list[str] = []

    def json(self, task: str, **kwargs):  # noqa: ANN003
        self.tasks.append(task)
        if not self.responses:
            return None
        return self.responses.pop(0)


def _docs() -> list[dict]:
    return [
        {
            "document_id": "doc_1",
            "name": "workplace_a_weekly_schedule.txt",
            "doc_family": "text",
            "media_type": "text/plain",
            "page_count": 1,
            "summary_text": json.dumps(
                {
                    "document_type_or_mix": "report",
                    "purpose": "Weekly staffing schedule for Workplace A shifts.",
                }
            ),
        },
        {
            "document_id": "doc_2",
            "name": "workplace_a_timeoff_schedule.txt",
            "doc_family": "text",
            "media_type": "text/plain",
            "page_count": 1,
            "summary_text": json.dumps(
                {
                    "document_type_or_mix": "report",
                    "purpose": "Time-off and rota schedule for Workplace A workers.",
                }
            ),
        },
        {
            "document_id": "doc_3",
            "name": "workplace_b_shift_plan.txt",
            "doc_family": "text",
            "media_type": "text/plain",
            "page_count": 1,
            "summary_text": json.dumps(
                {
                    "document_type_or_mix": "report",
                    "purpose": "Shift schedule for Workplace B employees.",
                }
            ),
        },
        {
            "document_id": "doc_4",
            "name": "workplace_b_holiday_plan.txt",
            "doc_family": "text",
            "media_type": "text/plain",
            "page_count": 1,
            "summary_text": json.dumps(
                {
                    "document_type_or_mix": "report",
                    "purpose": "Holiday and schedule planning for Workplace B.",
                }
            ),
        },
    ]


def _flatten_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    for group in payload.get("groups", []):
        ids.extend(group.get("document_ids", []))
    return ids


def test_catalogue_map_reduce_groups_documents_and_subgroups(tmp_path: Path) -> None:
    llm = StubLLM(
        responses=[
            {
                "groups": [
                    {
                        "label": "workplace schedules",
                        "description": "Scheduling documents for workplace operations.",
                        "members": [
                            {"document_id": "doc_1", "subgroup_label": "Workplace A"},
                            {"document_id": "doc_2", "subgroup_label": "Workplace A"},
                        ],
                    }
                ]
            },
            {
                "groups": [
                    {
                        "label": "workplace scheduling",
                        "description": "Schedules and shift plans grouped by location.",
                        "members": [
                            {"document_id": "doc_3", "subgroup_label": "Workplace B"},
                            {"document_id": "doc_4", "subgroup_label": "Workplace B"},
                        ],
                    }
                ]
            },
            {
                "catalogue_groups": [
                    {
                        "label": "workplace schedules",
                        "description": "Grouped by workplace and scheduling intent.",
                        "source_group_ids": ["b1_g1", "b2_g1"],
                    }
                ]
            },
        ]
    )

    payload = build_document_catalogue(
        documents=_docs(),
        llm=llm,
        cache_dir=tmp_path / "llm_cache",
        batch_size=2,
    )

    assert payload["grouping_method"] == "llm_map_reduce"
    assert payload["group_count"] == 1
    group = payload["groups"][0]
    assert group["label"] == "workplace schedules"
    subgroups = {sg["label"]: sg["document_count"] for sg in group["subgroups"]}
    assert subgroups["Workplace A"] == 2
    assert subgroups["Workplace B"] == 2
    assert sorted(_flatten_ids(payload)) == ["doc_1", "doc_2", "doc_3", "doc_4"]
    assert llm.tasks.count("document_catalogue_batch_grouping") == 2
    assert llm.tasks.count("document_catalogue_group_merge") == 1


def test_catalogue_falls_back_to_heuristics_when_llm_unavailable(tmp_path: Path) -> None:
    llm = StubLLM(responses=[], enabled=False)
    docs = _docs()
    docs.append(
        {
            "document_id": "doc_5",
            "name": "vendor_invoice.txt",
            "doc_family": "text",
            "media_type": "text/plain",
            "page_count": 1,
            "summary_text": json.dumps(
                {
                    "document_type_or_mix": "invoice",
                    "purpose": "Invoice for maintenance services.",
                }
            ),
        }
    )

    payload = build_document_catalogue(
        documents=docs,
        llm=llm,
        cache_dir=tmp_path / "llm_cache",
        batch_size=2,
    )

    assert payload["grouping_method"] == "heuristic"
    labels = {group["label"] for group in payload["groups"]}
    assert "workplace schedules" in labels
    assert "invoice documents" in labels
    flattened = sorted(_flatten_ids(payload))
    assert flattened == ["doc_1", "doc_2", "doc_3", "doc_4", "doc_5"]
