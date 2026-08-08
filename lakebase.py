"""Lakebase connection helpers for the Weather Intelligence homework.

The SQL schema is intentionally created manually from the numbered files in
``sql/``. This module only opens connections and executes queries.
"""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor

try:
    from sqlalchemy import create_engine
    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")
_workspace_client: WorkspaceClient | None = None


def _get_workspace_client() -> WorkspaceClient:
    global _workspace_client
    if _workspace_client is None:
        _workspace_client = WorkspaceClient()
    return _workspace_client


def lakebase_url() -> str:
    """Return LAKEBASE_URL locally or decode it from Databricks Secrets."""
    local_url = os.environ.get("LAKEBASE_URL")
    if local_url:
        return local_url

    secret = _get_workspace_client().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a psycopg2 connection that returns dictionary rows."""
    conn = psycopg2.connect(lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for optional pandas workflows."""
    if not _HAS_SQLALCHEMY:
        raise ImportError(
            "sqlalchemy is required for get_engine(). "
            "Install it with: %pip install sqlalchemy"
        )
    return create_engine(lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Execute a SELECT query and return rows as dictionaries."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Execute one write statement, commit, and return the affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def vector_literal(values: list[float]) -> str:
    """Serialize a Python vector into pgvector's text input format.

    e.g. [0.1, 0.2, 0.3] -> "[0.1,0.2,0.3]", for use with an explicit
    ``%s::vector`` cast in SQL. Centralized here - both app.py (query
    embeddings) and notebooks/ingest_weather_embeddings.ipynb (chunk
    embeddings) need to serialize a vector the same way, and previously each
    had its own copy of this one-liner.
    """
    return "[" + ",".join(str(float(value)) for value in values) + "]"