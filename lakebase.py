"""Lakebase connection helpers and schema creation for Homework 2."""

from __future__ import annotations

import base64
import os
import re
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor

_w: WorkspaceClient | None = None
_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_identifier(value: str) -> str:
    """Reject unsafe table names supplied through environment variables."""
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _workspace_client() -> WorkspaceClient:
    global _w
    if _w is None:
        _w = WorkspaceClient()
    return _w


def lakebase_url() -> str:
    """Use a local URL first, otherwise decode the Databricks secret value."""
    local_url = os.environ.get("LAKEBASE_URL")
    if local_url:
        return local_url

    secret = _workspace_client().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a psycopg2 connection whose rows are returned as dictionaries."""
    conn = psycopg2.connect(lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql: str, params: tuple | list | dict | None = None) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())


def run_write(sql: str, params: tuple | list | dict | None = None) -> int:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit()
            return cursor.rowcount


def ensure_weather_tables(
    documents_table: str = "weather_documents",
    embeddings_table: str = "weather_embeddings",
    embedding_dim: int = 384,
) -> None:
    """Create the raw-document and pgvector tables when they do not exist."""
    documents_table = safe_identifier(documents_table)
    embeddings_table = safe_identifier(embeddings_table)
    if embedding_dim != 384:
        raise ValueError("This homework repo is configured for 384-dimensional MiniLM vectors")

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {documents_table} (
                    id TEXT PRIMARY KEY,
                    location TEXT NOT NULL,
                    county TEXT,
                    query_latitude DOUBLE PRECISION,
                    query_longitude DOUBLE PRECISION,
                    source_type TEXT NOT NULL CHECK (source_type = 'alert'),
                    headline TEXT NOT NULL,
                    narrative_text TEXT NOT NULL,
                    severity TEXT,
                    severity_level SMALLINT CHECK (
                        severity_level IS NULL OR severity_level BETWEEN 1 AND 4
                    ),
                    flood_area_id TEXT,
                    river_or_sea TEXT,
                    ea_area_name TEXT,
                    ea_region_name TEXT,
                    source_url TEXT,
                    issued_at TIMESTAMPTZ,
                    effective_at TIMESTAMPTZ,
                    payload JSONB NOT NULL,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{documents_table}_county "
                f"ON {documents_table} (county)"
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{documents_table}_severity "
                f"ON {documents_table} (severity_level)"
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{documents_table}_effective_at "
                f"ON {documents_table} (effective_at DESC)"
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {embeddings_table} (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL
                        REFERENCES {documents_table}(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
                    chunk_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding VECTOR({embedding_dim}) NOT NULL,
                    model_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (document_id, chunk_index, model_name)
                )
                """
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{embeddings_table}_document_id "
                f"ON {embeddings_table} (document_id)"
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{embeddings_table}_embedding "
                f"ON {embeddings_table} USING hnsw (embedding vector_cosine_ops)"
            )
            conn.commit()
