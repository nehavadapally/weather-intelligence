"""Flask REST API for UK flood-warning vector search in Lakebase."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer

load_dotenv()

import lakebase
from weather_client import EnvironmentAgencyWeatherClient, WeatherClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uk-weather-intelligence")

app = Flask(__name__)

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
DEFAULT_LOCATIONS = [
    item.strip()
    for item in os.environ.get("WEATHER_LOCATIONS", "all").split(";")
    if item.strip()
]

_weather_model: SentenceTransformer | None = None


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def get_weather_model() -> SentenceTransformer:
    """Load the query embedding model once per application process."""
    global _weather_model
    if _weather_model is None:
        logger.info("Loading embedding model: %s", MODEL_NAME)
        model = SentenceTransformer(MODEL_NAME)
        dimension = int(model.get_sentence_embedding_dimension())
        if dimension != EMBEDDING_DIM:
            raise RuntimeError(
                f"Model {MODEL_NAME!r} produces {dimension} dimensions; "
                f"the database schema requires {EMBEDDING_DIM}."
            )
        _weather_model = model
    return _weather_model


def upsert_weather_documents(documents: list[dict[str, Any]]) -> int:
    """Batch-upsert normalized flood warnings into Lakebase."""
    if not documents:
        return 0

    rows = [
        (
            document["id"],
            document["location"],
            document.get("county"),
            document.get("query_latitude"),
            document.get("query_longitude"),
            document["source_type"],
            document["headline"],
            document["narrative_text"],
            document.get("severity"),
            document.get("severity_level"),
            document.get("flood_area_id"),
            document.get("river_or_sea"),
            document.get("ea_area_name"),
            document.get("ea_region_name"),
            document.get("source_url"),
            document.get("issued_at"),
            document.get("effective_at"),
            json.dumps(document["payload"]),
        )
        for document in documents
    ]

    sql = f"""
        INSERT INTO {DOCUMENTS_TABLE} (
            id, location, county, query_latitude, query_longitude,
            source_type, headline, narrative_text, severity, severity_level,
            flood_area_id, river_or_sea, ea_area_name, ea_region_name, source_url,
            issued_at, effective_at, payload
        ) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            location = EXCLUDED.location,
            county = EXCLUDED.county,
            query_latitude = EXCLUDED.query_latitude,
            query_longitude = EXCLUDED.query_longitude,
            source_type = EXCLUDED.source_type,
            headline = EXCLUDED.headline,
            narrative_text = EXCLUDED.narrative_text,
            severity = EXCLUDED.severity,
            severity_level = EXCLUDED.severity_level,
            flood_area_id = EXCLUDED.flood_area_id,
            river_or_sea = EXCLUDED.river_or_sea,
            ea_area_name = EXCLUDED.ea_area_name,
            ea_region_name = EXCLUDED.ea_region_name,
            source_url = EXCLUDED.source_url,
            issued_at = EXCLUDED.issued_at,
            effective_at = EXCLUDED.effective_at,
            payload = EXCLUDED.payload,
            synced_at = now()
    """

    with lakebase.get_connection() as conn:
        with conn.cursor() as cursor:
            execute_values(cursor, sql, rows, page_size=100)
            conn.commit()
    return len(rows)


@app.errorhandler(Exception)
def handle_exception(error: Exception):
    logger.exception("Unhandled request error")
    status = getattr(error, "code", 500)
    if not isinstance(status, int):
        status = 500
    return jsonify({"error": str(error)}), status


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "source": "Environment Agency flood API"})


@app.post("/weather/sync")
def sync_weather():
    """Harvest and upsert Environment Agency warning documents.

    Body example:
    {
      "locations": ["Somerset", "51.5074,-0.1278"],
      "limit": 50,
      "radius_km": 50,
      "min_severity": 3
    }
    """
    body = request.get_json(silent=True) or {}
    raw_locations = body.get("locations") or DEFAULT_LOCATIONS
    if isinstance(raw_locations, str):
        raw_locations = [raw_locations]
    if not isinstance(raw_locations, list):
        return jsonify({"error": "locations must be a list of strings"}), 400

    locations = [
        item.strip() for item in raw_locations if isinstance(item, str) and item.strip()
    ]
    if not locations:
        return jsonify({"error": "At least one location is required"}), 400

    limit = bounded_int(body.get("limit"), 50, 1, 500)
    radius_km = bounded_int(body.get("radius_km"), 50, 1, 200)
    min_severity = bounded_int(body.get("min_severity"), 3, 1, 4)

    lakebase.ensure_weather_tables(DOCUMENTS_TABLE, EMBEDDINGS_TABLE, EMBEDDING_DIM)
    client = EnvironmentAgencyWeatherClient()

    unique_documents: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []

    for location in locations:
        try:
            documents = client.fetch_documents(
                location,
                limit=limit,
                radius_km=radius_km,
                min_severity=min_severity,
            )
            for document in documents:
                unique_documents[document["id"]] = document
        except WeatherClientError as exc:
            logger.warning("Weather sync failed for %s: %s", location, exc)
            errors.append({"location": location, "error": str(exc)})

    if errors and len(errors) == len(locations):
        return jsonify({"error": "All Environment Agency requests failed", "details": errors}), 502

    synced = upsert_weather_documents(list(unique_documents.values()))
    message = (
        "No matching warnings were returned. Try location 'all' or set "
        "min_severity to 4 for a wider classroom-demo sample."
        if synced == 0
        else "Weather documents are ready for the embedding job."
    )

    return jsonify(
        {
            "synced": synced,
            "locations": locations,
            "limit_per_location": limit,
            "radius_km": radius_km,
            "min_severity": min_severity,
            "errors": errors,
            "message": message,
            "next_step": "Run notebooks/ingest_weather_embeddings.py",
            "attribution": client.attribution,
        }
    )


@app.get("/weather/documents")
def list_weather_documents():
    """Small inspection endpoint for checking the harvest stage."""
    limit = bounded_int(request.args.get("limit"), 20, 1, 100)
    rows = lakebase.run_query(
        f"""
        SELECT id, location, county, source_type, headline, severity,
               severity_level, issued_at, effective_at, synced_at
        FROM {DOCUMENTS_TABLE}
        ORDER BY COALESCE(effective_at, issued_at, synced_at) DESC
        LIMIT %s
        """,
        (limit,),
    )
    return jsonify(json_ready(rows))


@app.post("/weather/search")
def search_weather():
    """Embed a query and retrieve the most similar warning chunks."""
    body = request.get_json(silent=True) or {}
    query = str(body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    top_k = bounded_int(body.get("top_k"), 5, 1, 20)
    county = str(body.get("county") or "").strip()
    max_severity_level = body.get("max_severity_level")
    if max_severity_level is not None:
        max_severity_level = bounded_int(max_severity_level, 4, 1, 4)

    lakebase.ensure_weather_tables(DOCUMENTS_TABLE, EMBEDDINGS_TABLE, EMBEDDING_DIM)
    model = get_weather_model()
    vector = model.encode(query, normalize_embeddings=True).tolist()
    vector_literal = "[" + ",".join(str(float(value)) for value in vector) + "]"

    where_clauses: list[str] = []
    filter_params: list[Any] = []
    if county:
        where_clauses.append("d.county ILIKE %s")
        filter_params.append(f"%{county}%")
    if max_severity_level is not None:
        where_clauses.append("d.severity_level <= %s")
        filter_params.append(max_severity_level)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    sql = f"""
        SELECT
            d.id,
            d.location,
            d.county,
            d.source_type,
            d.headline,
            d.severity,
            d.severity_level,
            d.river_or_sea,
            d.source_url,
            d.issued_at,
            d.effective_at,
            e.chunk_index,
            e.chunk_text,
            1 - (e.embedding <=> %s::vector) AS similarity
        FROM {EMBEDDINGS_TABLE} e
        JOIN {DOCUMENTS_TABLE} d ON d.id = e.document_id
        {where_sql}
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
    """
    params: list[Any] = [vector_literal, *filter_params, vector_literal, top_k]
    rows = lakebase.run_query(sql, params)

    message = None
    if not rows:
        message = (
            "No matching embeddings are available. First call /weather/sync, "
            "then run notebooks/ingest_weather_embeddings.py."
        )

    return jsonify(
        {
            "query": query,
            "top_k": top_k,
            "count": len(rows),
            "filters": {
                "county": county or None,
                "max_severity_level": max_severity_level,
            },
            "results": json_ready(rows),
            "message": message,
        }
    )


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_RUN_PORT", "8000"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)
