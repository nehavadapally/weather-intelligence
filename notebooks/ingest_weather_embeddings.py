# Databricks notebook source
"""Chunk and embed Environment Agency warning documents with psycopg2.

This file runs as a plain Python script and can also be imported into a
Databricks notebook. It intentionally does not use Spark JDBC for writes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import lakebase
from text_utils import chunk_text

DOCUMENTS_TABLE = lakebase.safe_identifier(
    os.environ.get("WEATHER_DOCUMENTS_TABLE_NAME", "weather_documents")
)
EMBEDDINGS_TABLE = lakebase.safe_identifier(
    os.environ.get("WEATHER_EMBEDDINGS_TABLE_NAME", "weather_embeddings")
)
MODEL_NAME = os.environ.get(
    "WEATHER_EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)
EMBEDDING_DIM = 384


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_id(document_id: str, chunk_index: int, model_name: str) -> str:
    raw = f"{document_id}|{chunk_index}|{model_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_documents(limit: int | None = None) -> list[dict[str, Any]]:
    limit_sql = "LIMIT %s" if limit else ""
    params = (limit,) if limit else None
    return lakebase.run_query(
        f"""
        SELECT id, narrative_text
        FROM {DOCUMENTS_TABLE}
        WHERE narrative_text IS NOT NULL AND BTRIM(narrative_text) <> ''
        ORDER BY synced_at ASC
        {limit_sql}
        """,
        params,
    )


def load_existing_hashes() -> dict[str, set[str]]:
    rows = lakebase.run_query(
        f"""
        SELECT document_id, content_hash
        FROM {EMBEDDINGS_TABLE}
        WHERE model_name = %s
        GROUP BY document_id, content_hash
        """,
        (MODEL_NAME,),
    )
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(str(row["document_id"]), set()).add(str(row["content_hash"]))
    return result


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def run_ingestion(
    *,
    chunk_size: int,
    chunk_overlap: int,
    encode_batch_size: int,
    document_limit: int | None,
) -> tuple[int, int]:
    lakebase.ensure_weather_tables(DOCUMENTS_TABLE, EMBEDDINGS_TABLE, EMBEDDING_DIM)
    documents = load_documents(document_limit)
    if not documents:
        print("No weather documents found. Call POST /weather/sync first.")
        return 0, 0

    existing_hashes = load_existing_hashes()
    changed_documents: list[tuple[dict[str, Any], str]] = []
    for document in documents:
        digest = content_hash(str(document["narrative_text"]))
        if digest not in existing_hashes.get(str(document["id"]), set()):
            changed_documents.append((document, digest))

    if not changed_documents:
        print("All weather documents already have current embeddings.")
        return 0, 0

    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)
    dimension = int(model.get_sentence_embedding_dimension())
    if dimension != EMBEDDING_DIM:
        raise RuntimeError(
            f"Model emits {dimension} dimensions, but VECTOR({EMBEDDING_DIM}) is required"
        )

    chunk_records: list[dict[str, Any]] = []
    for document, digest in changed_documents:
        for chunk_index, text in enumerate(
            chunk_text(
                str(document["narrative_text"]),
                chunk_size=chunk_size,
                overlap=chunk_overlap,
            )
        ):
            chunk_records.append(
                {
                    "id": embedding_id(str(document["id"]), chunk_index, MODEL_NAME),
                    "document_id": str(document["id"]),
                    "chunk_index": chunk_index,
                    "chunk_text": text,
                    "content_hash": digest,
                }
            )

    texts = [record["chunk_text"] for record in chunk_records]
    vectors = model.encode(
        texts,
        batch_size=encode_batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,
    ).tolist()

    rows = [
        (
            record["id"],
            record["document_id"],
            record["chunk_index"],
            record["chunk_text"],
            record["content_hash"],
            vector_literal(vector),
            MODEL_NAME,
        )
        for record, vector in zip(chunk_records, vectors, strict=True)
    ]
    document_ids = [str(document["id"]) for document, _ in changed_documents]

    insert_sql = f"""
        INSERT INTO {EMBEDDINGS_TABLE} (
            id, document_id, chunk_index, chunk_text, content_hash,
            embedding, model_name, created_at
        ) VALUES %s
        ON CONFLICT (document_id, chunk_index, model_name) DO UPDATE SET
            id = EXCLUDED.id,
            chunk_text = EXCLUDED.chunk_text,
            content_hash = EXCLUDED.content_hash,
            embedding = EXCLUDED.embedding,
            created_at = now()
    """
    template = "(%s, %s, %s, %s, %s, %s::vector, %s, now())"

    with lakebase.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {EMBEDDINGS_TABLE} "
                "WHERE model_name = %s AND document_id = ANY(%s)",
                (MODEL_NAME, document_ids),
            )
            execute_values(
                cursor,
                insert_sql,
                rows,
                template=template,
                page_size=100,
            )
            conn.commit()

    print(
        f"Embedded {len(changed_documents)} documents into "
        f"{len(chunk_records)} chunks."
    )
    return len(changed_documents), len(chunk_records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    parser.add_argument("--encode-batch-size", type=int, default=32)
    parser.add_argument("--document-limit", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    run_ingestion(
        chunk_size=arguments.chunk_size,
        chunk_overlap=arguments.chunk_overlap,
        encode_batch_size=arguments.encode_batch_size,
        document_limit=arguments.document_limit,
    )
