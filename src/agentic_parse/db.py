from __future__ import annotations

import os

import psycopg2
import psycopg2.extensions
import psycopg2.extras


# Must match EMBEDDING_DIMS env var used in chunk_embed.py.
_VECTOR_DIMS = int(os.getenv("EMBEDDING_DIMS", "1536"))


class Connection:
    """Thin psycopg2 wrapper exposing the sqlite3 connection.execute() API.

    All callers use conn.execute(sql, params) returning a cursor with
    .fetchone() / .fetchall(), and conn.commit() — identical to the
    sqlite3 pattern the rest of the codebase already uses.
    """

    def __init__(self, pg_conn: psycopg2.extensions.connection) -> None:
        self._conn = pg_conn

    def execute(self, sql: str, params=()) -> psycopg2.extensions.cursor:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql, params if params else None)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def connect(dsn: str) -> Connection:
    pg_conn = psycopg2.connect(dsn)
    return Connection(pg_conn)


def _ensure_column(conn: Connection, table: str, column: str, definition: str) -> None:
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    if row:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_schema(conn: Connection) -> None:
    stmts = [
        "CREATE EXTENSION IF NOT EXISTS vector",

        f"""
        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY,
            sha256 TEXT UNIQUE NOT NULL,
            path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            doc_family TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            page_count INTEGER,
            has_text_layer INTEGER,
            audio_duration_seconds REAL,
            video_duration_seconds REAL,
            summary_status TEXT NOT NULL DEFAULT 'pending_transcription',
            status_ingest TEXT NOT NULL DEFAULT 'pending',
            status_ocr TEXT NOT NULL DEFAULT 'pending',
            status_embed TEXT NOT NULL DEFAULT 'pending',
            status_entities TEXT NOT NULL DEFAULT 'pending',
            status_asr TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS pages (
            page_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            source_tier TEXT,
            source_pointer TEXT,
            ocr_status TEXT NOT NULL DEFAULT 'pending',
            ocr_confidence REAL,
            text_path TEXT,
            page_hash TEXT,
            timestamp_start_ms INTEGER,
            timestamp_end_ms INTEGER,
            fallback_trigger_reason TEXT,
            fallback_region TEXT,
            page_type TEXT NOT NULL DEFAULT 'text',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(document_id, page_number),
            FOREIGN KEY(document_id) REFERENCES documents(document_id)
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            page_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text_path TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL,
            chunk_text_hash TEXT,
            embedding_model TEXT,
            embedding_version TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(page_id, chunk_index),
            FOREIGN KEY(page_id) REFERENCES pages(page_id),
            FOREIGN KEY(document_id) REFERENCES documents(document_id)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS vector_index (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_version TEXT NOT NULL,
            embedding vector({_VECTOR_DIMS}) NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id)
        )
        """,

        # HNSW index for fast approximate nearest-neighbour search (pgvector >= 0.5).
        "CREATE INDEX IF NOT EXISTS idx_vector_embedding ON vector_index USING hnsw (embedding vector_cosine_ops)",

        """
        CREATE TABLE IF NOT EXISTS relationships (
            edge_id TEXT PRIMARY KEY,
            subject_entity_id TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object_entity_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            page_number INTEGER,
            timestamp_start_ms INTEGER,
            timestamp_end_ms INTEGER,
            evidence_excerpt TEXT,
            confidence REAL NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS paystubs (
            paystub_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            page_number INTEGER,
            document_type TEXT,
            pay_period TEXT,
            pay_date TEXT,
            gross_pay REAL,
            net_pay REAL,
            currency TEXT,
            items_json TEXT,
            validation_status TEXT NOT NULL,
            validation_notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS fallback_events (
            event_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            page_id TEXT,
            source_tier TEXT NOT NULL,
            trigger_reason TEXT NOT NULL,
            region TEXT,
            page_hash TEXT,
            model_version TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS stage_metrics (
            id SERIAL PRIMARY KEY,
            stage TEXT NOT NULL,
            processed_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            token_input INTEGER NOT NULL DEFAULT 0,
            token_output INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS costly_calls (
            id SERIAL PRIMARY KEY,
            stage TEXT NOT NULL,
            step TEXT NOT NULL,
            location TEXT NOT NULL,
            call_type TEXT NOT NULL,
            document_id TEXT,
            page_id TEXT,
            chunk_id TEXT,
            provider TEXT,
            model_version TEXT,
            duration_ms REAL NOT NULL,
            token_input INTEGER NOT NULL DEFAULT 0,
            token_output INTEGER NOT NULL DEFAULT 0,
            cache_hit BOOLEAN,
            success BOOLEAN,
            metadata_json TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,

        "CREATE INDEX IF NOT EXISTS idx_documents_status_ocr ON documents(status_ocr)",
        "CREATE INDEX IF NOT EXISTS idx_documents_status_asr ON documents(status_asr)",
        "CREATE INDEX IF NOT EXISTS idx_documents_summary_status ON documents(summary_status)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id)",
        "CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(document_id)",
        "CREATE INDEX IF NOT EXISTS idx_costly_calls_stage ON costly_calls(stage)",
        "CREATE INDEX IF NOT EXISTS idx_costly_calls_created_at ON costly_calls(created_at)",
    ]

    for stmt in stmts:
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)

    # Lightweight migrations for databases created before certain columns existed.
    _ensure_column(conn, "documents", "audio_duration_seconds", "REAL")
    _ensure_column(conn, "documents", "video_duration_seconds", "REAL")
    _ensure_column(conn, "documents", "summary_status", "TEXT NOT NULL DEFAULT 'pending_transcription'")
    _ensure_column(conn, "documents", "status_asr", "TEXT NOT NULL DEFAULT 'pending'")

    _ensure_column(conn, "pages", "source_pointer", "TEXT")
    _ensure_column(conn, "pages", "timestamp_start_ms", "INTEGER")
    _ensure_column(conn, "pages", "timestamp_end_ms", "INTEGER")
    _ensure_column(conn, "pages", "fallback_trigger_reason", "TEXT")
    _ensure_column(conn, "pages", "fallback_region", "TEXT")
    _ensure_column(conn, "pages", "page_type", "TEXT NOT NULL DEFAULT 'text'")

    _ensure_column(conn, "chunks", "chunk_text_hash", "TEXT")
    _ensure_column(conn, "chunks", "embedding_model", "TEXT")
    _ensure_column(conn, "chunks", "embedding_version", "TEXT")

    _ensure_column(conn, "relationships", "timestamp_start_ms", "INTEGER")
    _ensure_column(conn, "relationships", "timestamp_end_ms", "INTEGER")
    _ensure_column(conn, "paystubs", "document_type", "TEXT")
    _ensure_column(conn, "paystubs", "items_json", "TEXT")

    conn.commit()
