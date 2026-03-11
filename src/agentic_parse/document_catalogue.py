from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DOC_BATCH_SIZE = 40
_GROUP_MERGE_BATCH = 120
_MAX_SHORT_SUMMARY_CHARS = 240
_PROPER_NOUN_PATTERN = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b")
_GENERIC_NOUNS = {
    # Months
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    # Days
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    # Document types
    "Invoice",
    "Receipt",
    "Report",
    "Letter",
    "Schedule",
    "Form",
    "Table",
    "Document",
    "File",
    "Attachment",
    "Summary",
    "Overview",
    "Section",
    "Appendix",
    "Exhibit",
    "Memo",
    "Notice",
    "Statement",
    "Agreement",
    # Common sentence-starting words (capitalized but not proper nouns)
    "The",
    "This",
    "That",
    "These",
    "Those",
    "A",
    "An",
    "All",
    "Each",
    "Both",
    "Some",
    "Any",
    "No",
    "There",
    "Here",
    "Where",
    "When",
    "What",
    "Which",
    "Who",
    "How",
    "Please",
    "Note",
    "See",
    "Per",
    "Re",
    # Generic org/place words
    "Service",
    "Services",
    "Center",
    "Centre",
    "Office",
    "Program",
    "Department",
    "Division",
    "Group",
    "Team",
    "Board",
    "Committee",
    "Item",
    "Items",
    "Date",
    "Page",
    "Total",
    "Amount",
}
_TYPE_ALIASES = {
    "comment_thread": "comment thread",
    "handwritten_note": "handwritten note",
}


def parse_summary_payload(text: str) -> tuple[dict | None, str]:
    stripped = text.strip()
    if not stripped:
        return None, ""
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload, stripped
    except json.JSONDecodeError:
        pass
    if "\n---\n" in stripped:
        head = stripped.split("\n---\n", 1)[0].strip()
        try:
            payload = json.loads(head)
            if isinstance(payload, dict):
                return payload, stripped
        except json.JSONDecodeError:
            pass
    return None, stripped


def _clean_text(value: str) -> str:
    return " ".join(value.strip().split())


def _first_sentence(value: str) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return ""
    split = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)
    return split[0].strip()


def _compact(value: str, limit: int = _MAX_SHORT_SUMMARY_CHARS) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= limit:
        return cleaned
    clipped = cleaned[: limit - 1].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped + "…"


def short_summary(summary_json: dict | None, summary_text: str) -> str:
    if summary_json:
        for key in ("purpose", "overall_purpose", "what_this_segment_contains"):
            value = summary_json.get(key)
            if isinstance(value, str) and value.strip():
                return _compact(_first_sentence(value))
    return _compact(_first_sentence(summary_text))


def document_type_hint(
    summary_json: dict | None, doc_family: str, media_type: str
) -> str:
    if summary_json:
        for key in ("document_type_or_mix", "document_type"):
            value = summary_json.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        if isinstance(summary_json.get("document_types_present"), list):
            candidates = [
                str(v).strip().lower()
                for v in summary_json["document_types_present"]
                if str(v).strip()
            ]
            if candidates:
                if len(candidates) == 1:
                    return candidates[0]
                return "mixed"
    if doc_family and doc_family != "other":
        return doc_family.strip().lower()
    return media_type.strip().lower() if media_type else "unknown"


def proper_nouns(text: str, max_items: int = 8) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for match in _PROPER_NOUN_PATTERN.findall(text):
        token = match.strip()
        if not token or token in _GENERIC_NOUNS:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(token)
        if len(items) >= max_items:
            break
    return items


def _normalize_label(value: str, fallback: str) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return fallback
    cleaned = cleaned.lower()
    return cleaned[:80].rstrip()


def _iter_batches(
    items: list[dict[str, Any]], batch_size: int
) -> list[list[dict[str, Any]]]:
    return [items[idx : idx + batch_size] for idx in range(0, len(items), batch_size)]


def _prepare_signals(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in documents:
        payload, raw = parse_summary_payload(str(row.get("summary_text", "")))
        short = short_summary(payload, raw)
        if not short:
            short = "No readable summary."
        dtype = document_type_hint(
            payload,
            str(row.get("doc_family", "")),
            str(row.get("media_type", "")),
        )
        nouns = proper_nouns(f"{short} {row.get('name', '')}")
        prepared.append(
            {
                "document_id": str(row["document_id"]),
                "name": str(row.get("name") or row["document_id"]),
                "doc_family": str(row.get("doc_family", "")),
                "media_type": str(row.get("media_type", "")),
                "page_count": row.get("page_count"),
                "short_summary": short,
                "document_type_hint": dtype,
                "proper_nouns": nouns,
            }
        )
    return prepared


def _heuristic_group_label(doc: dict[str, Any]) -> str:
    text = doc.get("short_summary", "").lower()
    if "schedule" in text:
        return "workplace schedules"
    hint = str(doc.get("document_type_hint", "")).strip().lower()
    if hint and hint not in {"unknown", "mixed"}:
        normalized = _TYPE_ALIASES.get(hint, hint.replace("_", " "))
        return f"{normalized} documents"
    family = str(doc.get("doc_family", "")).strip().lower()
    if family and family != "other":
        return f"{family} documents"
    return "uncategorized documents"


def _heuristic_groups(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        grouped[_heuristic_group_label(doc)].append(doc)

    groups: list[dict[str, Any]] = []
    for label, items in grouped.items():
        subgrouped: dict[str, list[str]] = defaultdict(list)
        for item in items:
            noun = item.get("proper_nouns", [])
            subgroup = noun[0] if noun else ""
            if subgroup:
                subgrouped[subgroup].append(item["document_id"])
        subgroups = [
            {
                "label": key,
                "document_ids": sorted(value),
                "document_count": len(value),
            }
            for key, value in sorted(
                subgrouped.items(), key=lambda x: (-len(x[1]), x[0].lower())
            )
            if len(value) >= 2
        ]
        groups.append(
            {
                "label": label,
                "description": "Grouped by inferred document type and repeated entities.",
                "document_ids": sorted([d["document_id"] for d in items]),
                "document_count": len(items),
                "subgroups": subgroups,
            }
        )
    groups.sort(key=lambda g: (-g["document_count"], g["label"]))
    return groups


def _validate_source_groups(
    batch_docs: list[dict[str, Any]],
    payload: dict[str, Any] | None,
    batch_idx: int,
) -> list[dict[str, Any]]:
    doc_ids = {d["document_id"] for d in batch_docs}
    assigned: set[str] = set()
    groups: list[dict[str, Any]] = []

    raw_groups = payload.get("groups") if isinstance(payload, dict) else None
    if isinstance(raw_groups, list):
        for i, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, dict):
                continue
            raw_members = raw_group.get("members")
            if not isinstance(raw_members, list):
                continue
            members: list[dict[str, str]] = []
            for member in raw_members:
                if not isinstance(member, dict):
                    continue
                doc_id = str(member.get("document_id", "")).strip()
                if doc_id not in doc_ids or doc_id in assigned:
                    continue
                subgroup = _clean_text(str(member.get("subgroup_label", "")))[:80]
                members.append({"document_id": doc_id, "subgroup_label": subgroup})
                assigned.add(doc_id)
            if not members:
                continue
            label = _normalize_label(
                str(raw_group.get("label", "")), "uncategorized documents"
            )
            desc = _compact(str(raw_group.get("description", "")), limit=180)
            groups.append(
                {
                    "source_group_id": f"b{batch_idx + 1}_g{i + 1}",
                    "label": label,
                    "description": desc,
                    "members": members,
                }
            )

    missing = [doc_id for doc_id in sorted(doc_ids) if doc_id not in assigned]
    if missing:
        groups.append(
            {
                "source_group_id": f"b{batch_idx + 1}_g_fallback",
                "label": "uncategorized documents",
                "description": "Fallback assignment for documents not returned by the LLM.",
                "members": [
                    {"document_id": doc_id, "subgroup_label": ""} for doc_id in missing
                ],
            }
        )
    return groups


def _group_batch_with_llm(
    llm: Any,
    cache_dir: Path,
    batch_docs: list[dict[str, Any]],
    batch_idx: int,
    total_batches: int,
) -> dict[str, Any] | None:
    if not getattr(llm, "enabled", False):
        return None
    prompt_docs = [
        {
            "document_id": doc["document_id"],
            "name": doc["name"],
            "document_type_hint": doc["document_type_hint"],
            "doc_family": doc["doc_family"],
            "page_count": doc.get("page_count"),
            "proper_nouns": doc["proper_nouns"][:6],
            "short_summary": doc["short_summary"],
        }
        for doc in batch_docs
    ]
    user_prompt = (
        f"Batch {batch_idx + 1} of {total_batches}. Group these documents into a catalogue taxonomy.\n"
        "Primary signals: purpose alignment, document type, repeated proper nouns.\n"
        "You must assign each document exactly once.\n\n"
        "Return strict JSON with this schema:\n"
        "{\n"
        '  "groups": [\n'
        "    {\n"
        '      "label": "lowercase category name, 2-5 words",\n'
        '      "description": "one sentence",\n'
        '      "members": [\n'
        '        {"document_id": "id", "subgroup_label": "optional proper-noun subgroup"}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"DOCUMENTS:\n{json.dumps(prompt_docs, ensure_ascii=False)}\n\n"
        "Return JSON only."
    )
    return llm.json(
        task="document_catalogue_batch_grouping",
        cache_dir=cache_dir,
        system_prompt=(
            "You are building a scalable document catalogue. "
            "Group documents conservatively and consistently using only supplied metadata."
        ),
        user_prompt=user_prompt,
        max_output_tokens=1600,
    )


def _merge_group_descriptors_with_llm(
    llm: Any,
    cache_dir: Path,
    descriptors: list[dict[str, Any]],
    chunk_idx: int,
    total_chunks: int,
) -> dict[str, Any] | None:
    if not getattr(llm, "enabled", False):
        return None
    user_prompt = (
        f"Descriptor chunk {chunk_idx + 1} of {total_chunks}. Merge source groups into final catalogue groups.\n"
        "Group by shared purpose, document type, and proper nouns. Keep labels concise and lowercase.\n"
        "Assign each source_group_id exactly once.\n\n"
        "Return strict JSON with this schema:\n"
        "{\n"
        '  "catalogue_groups": [\n'
        "    {\n"
        '      "label": "lowercase category name",\n'
        '      "description": "one sentence",\n'
        '      "source_group_ids": ["b1_g1", "b2_g2"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"SOURCE GROUPS:\n{json.dumps(descriptors, ensure_ascii=False)}\n\n"
        "Return JSON only."
    )
    return llm.json(
        task="document_catalogue_group_merge",
        cache_dir=cache_dir,
        system_prompt=(
            "You normalize batch-level document groups into coherent top-level catalogue categories."
        ),
        user_prompt=user_prompt,
        max_output_tokens=1400,
    )


def _default_merge(source_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in source_groups:
        key = _normalize_label(str(group.get("label", "")), "uncategorized documents")
        merged[key].append(group)
    out: list[dict[str, Any]] = []
    for label, groups in merged.items():
        source_ids = [str(g["source_group_id"]) for g in groups]
        out.append(
            {
                "label": label,
                "description": "Merged from batch catalogue groups.",
                "source_group_ids": source_ids,
            }
        )
    out.sort(key=lambda item: item["label"])
    return out


def _merge_source_groups(
    llm: Any,
    cache_dir: Path,
    source_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(source_groups) <= 1:
        only = source_groups[0] if source_groups else None
        if not only:
            return []
        return [
            {
                "label": only["label"],
                "description": only.get("description", ""),
                "source_group_ids": [only["source_group_id"]],
            }
        ]

    descriptors = [
        {
            "source_group_id": group["source_group_id"],
            "label": group["label"],
            "description": group.get("description", ""),
            "document_count": len(group.get("members", [])),
            "sample_document_ids": [
                m["document_id"] for m in group.get("members", [])[:5]
            ],
        }
        for group in source_groups
    ]

    chunks = _iter_batches(descriptors, _GROUP_MERGE_BATCH)
    merged: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for idx, chunk in enumerate(chunks):
        payload = _merge_group_descriptors_with_llm(
            llm, cache_dir, chunk, idx, len(chunks)
        )
        raw = payload.get("catalogue_groups") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            continue
        for group in raw:
            if not isinstance(group, dict):
                continue
            ids = []
            for src in group.get("source_group_ids", []):
                src_id = str(src).strip()
                if not src_id or src_id in assigned:
                    continue
                ids.append(src_id)
                assigned.add(src_id)
            if not ids:
                continue
            merged.append(
                {
                    "label": _normalize_label(
                        str(group.get("label", "")), "uncategorized documents"
                    ),
                    "description": _compact(
                        str(group.get("description", "")), limit=180
                    ),
                    "source_group_ids": ids,
                }
            )

    expected_ids = {d["source_group_id"] for d in descriptors}
    if not merged or assigned != expected_ids:
        return _default_merge(source_groups)
    return merged


def build_document_catalogue(
    *,
    documents: list[dict[str, Any]],
    llm: Any,
    cache_dir: Path,
    batch_size: int = _DOC_BATCH_SIZE,
) -> dict[str, Any]:
    prepared = _prepare_signals(documents)
    if not prepared:
        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "grouping_method": "empty",
            "document_count": 0,
            "group_count": 0,
            "groups": [],
        }

    grouped_with_llm = False
    source_groups: list[dict[str, Any]] = []
    batches = _iter_batches(prepared, max(1, int(batch_size)))
    for idx, batch_docs in enumerate(batches):
        payload = _group_batch_with_llm(llm, cache_dir, batch_docs, idx, len(batches))
        normalized = _validate_source_groups(batch_docs, payload, idx)
        if payload and any(
            not str(g.get("source_group_id", "")).endswith("_fallback")
            for g in normalized
        ):
            grouped_with_llm = True
        source_groups.extend(normalized)

    if not grouped_with_llm:
        groups = _heuristic_groups(prepared)
        method = "heuristic"
    elif not source_groups:
        groups = _heuristic_groups(prepared)
        method = "heuristic"
    else:
        merged_groups = _merge_source_groups(llm, cache_dir, source_groups)
        merged_lookup = {g["source_group_id"]: g for g in source_groups}
        id_to_doc = {d["document_id"]: d for d in prepared}
        groups = []
        for merged in merged_groups:
            members: list[dict[str, str]] = []
            for source_id in merged["source_group_ids"]:
                source = merged_lookup.get(source_id)
                if not source:
                    continue
                members.extend(source.get("members", []))
            doc_ids: list[str] = []
            subgroup_buckets: dict[str, list[str]] = defaultdict(list)
            seen_ids: set[str] = set()
            for member in members:
                doc_id = member["document_id"]
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                doc_ids.append(doc_id)
                subgroup = _clean_text(member.get("subgroup_label", ""))[:80]
                if subgroup:
                    subgroup_buckets[subgroup].append(doc_id)
            if not doc_ids:
                continue

            doc_ids.sort(
                key=lambda doc_id: id_to_doc.get(doc_id, {}).get("name", doc_id).lower()
            )
            subgroups = [
                {
                    "label": subgroup,
                    "document_ids": sorted(
                        ids,
                        key=lambda doc_id: id_to_doc.get(doc_id, {})
                        .get("name", doc_id)
                        .lower(),
                    ),
                    "document_count": len(ids),
                }
                for subgroup, ids in sorted(
                    subgroup_buckets.items(), key=lambda x: (-len(x[1]), x[0].lower())
                )
                if len(ids) >= 2
            ]
            groups.append(
                {
                    "label": merged["label"],
                    "description": merged.get("description", ""),
                    "document_ids": doc_ids,
                    "document_count": len(doc_ids),
                    "subgroups": subgroups,
                }
            )
        if not groups:
            groups = _heuristic_groups(prepared)
            method = "heuristic"
        else:
            groups.sort(key=lambda g: (-g["document_count"], g["label"]))
            method = "llm_map_reduce" if grouped_with_llm else "heuristic"

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "grouping_method": method,
        "document_count": len(prepared),
        "group_count": len(groups),
        "groups": groups,
    }
