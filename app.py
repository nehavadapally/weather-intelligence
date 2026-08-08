"""Flask REST API for NWS weather ingestion and Lakebase vector search."""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template
from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer

load_dotenv()

import lakebase
from weather_client import NWSWeatherClient, WeatherClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nws-weather-intelligence")

app = Flask(__name__)

DOCUMENTS_TABLE = os.environ.get("WEATHER_DOCUMENTS_TABLE_NAME", "weather_documents")
EMBEDDINGS_TABLE = os.environ.get("WEATHER_EMBEDDINGS_TABLE_NAME", "weather_embeddings")
MODEL_NAME = os.environ.get(
    "WEATHER_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
DEFAULT_LOCATIONS = [
    value.strip()
    for value in os.environ.get("WEATHER_LOCATIONS", "Chicago, IL;Austin, TX").split(";")
    if value.strip()
]

_embedding_model: SentenceTransformer | None = None


def get_embedding_model() -> SentenceTransformer:
    """Load the 384-dimensional model once and reuse it for all searches."""
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading embedding model: %s", MODEL_NAME)
        _embedding_model = SentenceTransformer(MODEL_NAME)
    return _embedding_model


def _upsert_weather_documents(documents: list[dict]) -> int:
    """Upsert normalised NWS documents with one batched psycopg2 write."""
    if not documents:
        return 0

    rows = [
        (
            item["id"],
            item["location"],
            item.get("latitude"),
            item.get("longitude"),
            item["source_type"],
            item["headline"],
            item["narrative_text"],
            item.get("issued_at"),
            item.get("effective_at"),
            json.dumps(item["payload"]),
        )
        for item in documents
    ]

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"""
                INSERT INTO {DOCUMENTS_TABLE} (
                    id, location, latitude, longitude, source_type,
                    headline, narrative_text, issued_at, effective_at,
                    payload, synced_at
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    location = EXCLUDED.location,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    source_type = EXCLUDED.source_type,
                    headline = EXCLUDED.headline,
                    narrative_text = EXCLUDED.narrative_text,
                    issued_at = EXCLUDED.issued_at,
                    effective_at = EXCLUDED.effective_at,
                    payload = EXCLUDED.payload,
                    synced_at = now()
                WHERE {DOCUMENTS_TABLE}.narrative_text IS DISTINCT FROM EXCLUDED.narrative_text
                   OR {DOCUMENTS_TABLE}.headline IS DISTINCT FROM EXCLUDED.headline
                   OR {DOCUMENTS_TABLE}.payload IS DISTINCT FROM EXCLUDED.payload
                """,
                rows,
                template=(
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())"
                ),
                page_size=100,
            )
            conn.commit()
    return len(documents)


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.post("/weather/sync")
def weather_sync():
    """Harvest NWS alerts and forecasts and upsert them into Lakebase.

    Body: {"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}
    """
    body = request.get_json(silent=True) or {}
    locations = body.get("locations") or DEFAULT_LOCATIONS
    limit = max(1, min(int(body.get("limit", 50)), 200))

    if not isinstance(locations, list) or not locations:
        return jsonify({"error": "locations must be a non-empty list"}), 400

    client = NWSWeatherClient()
    documents_by_id: dict[str, dict] = {}
    errors: list[dict[str, str]] = []

    for location in locations:
        if not isinstance(location, str) or not location.strip():
            errors.append({"location": str(location), "error": "Invalid location"})
            continue
        try:
            for document in client.fetch_documents(location, limit=limit):
                documents_by_id[document["id"]] = document
        except WeatherClientError as exc:
            errors.append({"location": location, "error": str(exc)})

    documents = list(documents_by_id.values())
    synced = _upsert_weather_documents(documents)
    return jsonify(
        {
            "synced": synced,
            "unique_documents": len(documents),
            "locations": locations,
            "errors": errors,
        }
    )


@app.get("/weather/documents")
def weather_documents():
    limit = max(1, min(int(request.args.get("limit", 50)), 200))
    rows = lakebase.run_query(
        f"""
        SELECT id, location, latitude, longitude, source_type, headline,
               narrative_text, issued_at, effective_at, synced_at
        FROM {DOCUMENTS_TABLE}
        ORDER BY synced_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return jsonify(rows)


@app.post("/weather/search")
def weather_search():
    """Embed a query and rank weather chunks with cosine similarity."""
    body = request.get_json(silent=True) or {}
    query = str(body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        top_k = int(body.get("top_k", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "top_k must be an integer"}), 400
    top_k = max(1, min(top_k, 20))

    source_type = body.get("source_type")
    if source_type not in (None, "alert", "forecast"):
        return jsonify({"error": "source_type must be alert or forecast"}), 400

    vector = get_embedding_model().encode(query).tolist()
    vector_literal = "[" + ",".join(str(float(value)) for value in vector) + "]"

    source_clause = "AND d.source_type = %s" if source_type else ""
    params: list[object] = [vector_literal, MODEL_NAME]
    if source_type:
        params.append(source_type)
    params.extend([vector_literal, top_k])

    rows = lakebase.run_query(
        f"""
        SELECT d.id,
               d.location,
               d.latitude,
               d.longitude,
               d.source_type,
               d.headline,
               d.narrative_text,
               d.issued_at,
               d.effective_at,
               e.chunk_index,
               e.chunk_text,
               1 - (e.embedding <=> %s::vector) AS similarity
        FROM {EMBEDDINGS_TABLE} e
        JOIN {DOCUMENTS_TABLE} d ON d.id = e.document_id
        WHERE e.model_name = %s
          {source_clause}
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
        """,
        tuple(params),
    )

    return jsonify(
        {
            "query": query,
            "top_k": top_k,
            "count": len(rows),
            "results": rows,
            "message": (
                None
                if rows
                else "No embeddings found. Run /weather/sync and then the embedding notebook."
            ),
        }
    )


@app.errorhandler(Exception)
def handle_exception(error):
    logger.exception("Unhandled application error")
    status = getattr(error, "code", 500)
    if not isinstance(status, int):
        status = 500
    return jsonify({"error": str(error)}), status


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_RUN_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_RUN_PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true",
    )
