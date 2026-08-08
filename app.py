"""Flask REST API for NWS weather ingestion and Lakebase vector search."""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from sentence_transformers import SentenceTransformer

load_dotenv()

import lakebase
from llm_summary import summarize_search_results
from weather_client import _KNOWN_US_PLACES
from weather_sync import run_weather_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nws-weather-intelligence")

app = Flask(__name__)

DOCUMENTS_TABLE = os.environ.get(
    "WEATHER_DOCUMENTS_TABLE_NAME",
    "weather_documents",
)
EMBEDDINGS_TABLE = os.environ.get(
    "WEATHER_EMBEDDINGS_TABLE_NAME",
    "weather_embeddings",
)
MODEL_NAME = os.environ.get(
    "WEATHER_EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)

_ALLOWED_SOURCE_TYPES = (None, "alert", "forecast")

_embedding_model: SentenceTransformer | None = None


def get_embedding_model() -> SentenceTransformer:
    """Load the 384-dimensional model once and reuse it."""
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading embedding model: %s", MODEL_NAME)
        _embedding_model = SentenceTransformer(MODEL_NAME)
    return _embedding_model


class SearchParamError(ValueError):
    """Invalid /weather/search input."""


def _display_location_name(location_key: str) -> str:
    """Convert a lower-case catalogue key into a UI label."""
    city, state = [part.strip() for part in location_key.rsplit(",", 1)]
    return f"{city.title()}, {state.upper()}"


def _parse_search_params(
    query: Any,
    top_k: Any,
    source_type: Any,
    location: Any,
) -> tuple[str, int, str | None, str | None]:
    """Validate and normalise POST and GET search parameters."""
    query = str(query or "").strip()
    if not query:
        raise SearchParamError("query is required")

    try:
        top_k = int(top_k) if top_k not in (None, "") else 5
    except (TypeError, ValueError) as exc:
        raise SearchParamError("top_k must be an integer") from exc
    top_k = max(1, min(top_k, 20))

    source_type = source_type or None
    if source_type not in _ALLOWED_SOURCE_TYPES:
        raise SearchParamError("source_type must be alert or forecast")

    location = str(location or "").strip() or None
    return query, top_k, source_type, location


def _search_weather_chunks(
    query: str,
    top_k: int,
    source_type: str | None,
    location: str | None,
) -> list[dict]:
    """Run pgvector cosine-similarity retrieval."""
    vector = get_embedding_model().encode(query).tolist()
    vector_literal = lakebase.vector_literal(vector)

    clauses = ["e.model_name = %s"]
    params: list[object] = [vector_literal, MODEL_NAME]

    if source_type:
        clauses.append("d.source_type = %s")
        params.append(source_type)

    if location:
        clauses.append("d.location = %s")
        params.append(location)

    params.extend([vector_literal, top_k])
    where_sql = "\n          AND ".join(clauses)

    return lakebase.run_query(
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
        JOIN {DOCUMENTS_TABLE} d
          ON d.id = e.document_id
        WHERE {where_sql}
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
        """,
        tuple(params),
    )


def _search_response(
    query: str,
    top_k: int,
    source_type: str | None,
    location: str | None,
    rows: list[dict],
    summary: str | None,
) -> dict:
    """Shape the response returned by both search routes."""
    return {
        "query": query,
        "top_k": top_k,
        "source_type": source_type,
        "location": location,
        "count": len(rows),
        "results": rows,
        "summary": summary,
        "message": (
            None
            if rows
            else (
                "No matching embeddings found. Sync weather documents and "
                "run the embedding notebook before searching."
            )
        ),
    }


@app.get("/")
def index():
    """Serve the two-tab weather interface."""
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/weather/location-options")
def weather_location_options():
    """Return locally supported US city/state suggestions.

    Selecting one of these values avoids a separate geocoder request.
    """
    options = []
    seen: set[str] = set()

    for key, coordinates in sorted(_KNOWN_US_PLACES.items()):
        name = _display_location_name(key)
        if name in seen:
            continue
        seen.add(name)
        latitude, longitude = coordinates
        options.append(
            {
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    return jsonify(options)


@app.get("/weather/locations")
def weather_locations():
    """Summarise the locations already stored in weather_documents."""
    rows = lakebase.run_query(
        f"""
        SELECT location,
               MIN(latitude) AS latitude,
               MIN(longitude) AS longitude,
               COUNT(*)::int AS document_count,
               COUNT(*) FILTER (
                   WHERE source_type = 'alert'
               )::int AS alert_count,
               COUNT(*) FILTER (
                   WHERE source_type = 'forecast'
               )::int AS forecast_count,
               MAX(synced_at) AS last_synced_at
        FROM {DOCUMENTS_TABLE}
        GROUP BY location
        ORDER BY MAX(synced_at) DESC, location ASC
        """
    )
    return jsonify(rows)


@app.post("/weather/sync")
def weather_sync():
    """Harvest NWS alerts and forecasts and upsert them into Lakebase.

    Required homework contract:
    {"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}
    """
    body = request.get_json(silent=True) or {}
    try:
        result = run_weather_sync(
            body.get("locations"),
            body.get("limit", 50),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.get("/weather/documents")
def weather_documents():
    """Inspect recently synchronised raw weather documents."""
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400

    limit = max(1, min(limit, 200))
    rows = lakebase.run_query(
        f"""
        SELECT id,
               location,
               latitude,
               longitude,
               source_type,
               headline,
               narrative_text,
               issued_at,
               effective_at,
               synced_at
        FROM {DOCUMENTS_TABLE}
        ORDER BY synced_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return jsonify(rows)


@app.post("/weather/search")
def weather_search():
    """Embed a query and rank weather chunks by cosine similarity.

    Required homework body:
    {"query": "risk of flooding near rivers", "top_k": 5}

    Optional UI filters:
    source_type: "alert" or "forecast"
    location: an exact stored location label
    summarize: true to request an optional LLM summary
    """
    body = request.get_json(silent=True) or {}

    try:
        query, top_k, source_type, location = _parse_search_params(
            body.get("query"),
            body.get("top_k", 5),
            body.get("source_type"),
            body.get("location"),
        )
    except SearchParamError as exc:
        return jsonify({"error": str(exc)}), 400

    rows = _search_weather_chunks(
        query,
        top_k,
        source_type,
        location,
    )
    summary = (
        summarize_search_results(query, rows)
        if body.get("summarize")
        else None
    )

    return jsonify(
        _search_response(
            query,
            top_k,
            source_type,
            location,
            rows,
            summary,
        )
    )


@app.get("/weather/search")
def weather_search_get():
    """Browser-friendly variant of weather semantic search."""
    args = request.args

    try:
        query, top_k, source_type, location = _parse_search_params(
            args.get("query"),
            args.get("top_k", 5),
            args.get("source_type"),
            args.get("location"),
        )
    except SearchParamError as exc:
        return jsonify({"error": str(exc)}), 400

    summarize = args.get("summarize", "false").strip().lower() in (
        "true",
        "1",
        "yes",
    )

    rows = _search_weather_chunks(
        query,
        top_k,
        source_type,
        location,
    )
    summary = summarize_search_results(query, rows) if summarize else None

    return jsonify(
        _search_response(
            query,
            top_k,
            source_type,
            location,
            rows,
            summary,
        )
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
