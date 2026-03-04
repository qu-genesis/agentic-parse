from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import sqlite3

from .config import Settings
from .telemetry import record_stage_metric
from .utils import append_jsonl, atomic_write_text


EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "deterministic-local")
EMBEDDING_VERSION = os.getenv("EMBEDDING_VERSION", "v1")


def _chunk_id(page_id: str, index: int) -> str:
    return f"{page_id}_c{index:04d}"


def _token_estimate(text: str) -> int:
    return max(1, len(text) // 4)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fake_embedding(text: str, dims: int = 32) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = []
    for i in range(dims):
        b = digest[i % len(digest)]
        values.append((b / 255.0) * 2.0 - 1.0)
    return values


def _split_text(text: str, max_chars: int = 2000, overlap: int = 200) -> list[tuple[int, int, str]]:
    clean = text.strip()
    if not clean:
        return []
    spans: list[tuple[int, int, str]] = []
    start = 0
    size = len(clean)
    while start < size:
        end = min(size, start + max_chars)
        chunk = clean[start:end]
        spans.append((start, end, chunk))
        if end == size:
            break
        start = max(0, end - overlap)
    return spans


def _upsert_vector_index(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str,
    vector: list[float],
) -> None:
    vector_json = json.dumps(vector)
    conn.execute(
        """
        INSERT INTO vector_index (chunk_id, document_id, embedding_model, embedding_version, vector_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chunk_id) DO UPDATE SET
            embedding_model = excluded.embedding_model,
            embedding_version = excluded.embedding_version,
            vector_json = excluded.vector_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (chunk_id, document_id, EMBEDDING_MODEL, EMBEDDING_VERSION, vector_json),
    )
    append_jsonl(
        settings.vector_index_jsonl,
        {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_version": EMBEDDING_VERSION,
            "vector": vector,
        },
    )


def chunk_and_embed(settings: Settings, conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT p.page_id, p.document_id, p.page_number, p.text_path
        FROM pages p
        ORDER BY p.document_id, p.page_number
        """
    ).fetchall()

    inserted_or_updated = 0
    skipped = 0
    touched_docs: set[str] = set()
    for row in rows:
        text_path = Path(row["text_path"])
        if not text_path.exists():
            continue
        text = text_path.read_text(encoding="utf-8")
        spans = _split_text(text)
        for idx, (start, end, chunk_text) in enumerate(spans, start=1):
            chunk_id = _chunk_id(row["page_id"], idx)
            text_hash = _text_hash(chunk_text)
            existing = conn.execute(
                "SELECT chunk_text_hash, embedding_version FROM chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()

            out = settings.chunks_dir / row["document_id"] / f"{row['page_id']}_c{idx:04d}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists() or out.read_text(encoding="utf-8") != chunk_text:
                atomic_write_text(out, chunk_text)

            vector = _fake_embedding(chunk_text)
            vector_json = json.dumps(vector)

            if existing and existing["chunk_text_hash"] == text_hash and existing["embedding_version"] == EMBEDDING_VERSION:
                _upsert_vector_index(
                    settings,
                    conn,
                    chunk_id=chunk_id,
                    document_id=row["document_id"],
                    vector=vector,
                )
                skipped += 1
                continue

            conn.execute(
                """
                INSERT INTO chunks (
                    chunk_id, page_id, document_id, page_number, chunk_index,
                    text_path, char_start, char_end, token_estimate,
                    chunk_text_hash, embedding_vector, embedding_model, embedding_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    text_path = excluded.text_path,
                    char_start = excluded.char_start,
                    char_end = excluded.char_end,
                    token_estimate = excluded.token_estimate,
                    chunk_text_hash = excluded.chunk_text_hash,
                    embedding_vector = excluded.embedding_vector,
                    embedding_model = excluded.embedding_model,
                    embedding_version = excluded.embedding_version,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chunk_id,
                    row["page_id"],
                    row["document_id"],
                    row["page_number"],
                    idx,
                    out.as_posix(),
                    start,
                    end,
                    _token_estimate(chunk_text),
                    text_hash,
                    vector_json,
                    EMBEDDING_MODEL,
                    EMBEDDING_VERSION,
                ),
            )
            _upsert_vector_index(
                settings,
                conn,
                chunk_id=chunk_id,
                document_id=row["document_id"],
                vector=vector,
            )
            inserted_or_updated += 1
            touched_docs.add(row["document_id"])

    for doc_id in touched_docs:
        conn.execute(
            "UPDATE documents SET status_embed = 'done', updated_at = CURRENT_TIMESTAMP WHERE document_id = ?",
            (doc_id,),
        )

    record_stage_metric(
        settings,
        conn,
        "chunk_embed",
        processed=inserted_or_updated,
        skipped=skipped,
        failed=0,
        metadata={"embedding_model": EMBEDDING_MODEL, "embedding_version": EMBEDDING_VERSION},
    )
    conn.commit()
    return inserted_or_updated


def retrieve_top_k_chunks(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
    max_chunks: int,
    max_tokens: int,
) -> list[sqlite3.Row]:
    if top_k > max_chunks:
        raise ValueError(f"top_k ({top_k}) exceeds max_chunks ({max_chunks})")

    qvec = _fake_embedding(query)
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.document_id, c.page_number, c.text_path, c.token_estimate, c.embedding_vector
        FROM chunks c
        ORDER BY c.updated_at DESC
        LIMIT 5000
        """
    ).fetchall()

    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        try:
            vec = json.loads(row["embedding_vector"])
            if not isinstance(vec, list):
                continue
            dot = sum(float(a) * float(b) for a, b in zip(qvec, vec))
            scored.append((dot, row))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    selected: list[sqlite3.Row] = []
    token_sum = 0
    for _, row in scored:
        if len(selected) >= top_k:
            break
        projected = token_sum + int(row["token_estimate"])
        if projected > max_tokens:
            raise ValueError(
                f"retrieval token budget exceeded: {projected} > {max_tokens} (reduce top_k or chunk size)"
            )
        selected.append(row)
        token_sum = projected

    return selected
