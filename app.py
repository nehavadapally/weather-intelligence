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
from llm_summary import (
    summarize_search_results,
    summary_is_configured,
)
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
    """Convert a lower-case catalogue key into a display label."""
    city, state = [
        part.strip()
        for part in location_key.rsplit(",", 1)
    ]

    return f"{city.title()}, {state.upper()}"


def _parse_boolean(value: Any, default: bool = False) -> bool:
    """Convert common JSON and query-string values into a boolean."""
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
    }


def _parse_search_params(
    query: Any,
    top_k: Any,
    source_type: Any,
    location: Any,
) -> tuple[str, int, str | None, str | None]:
    """Validate and normalise search parameters."""
    query = str(query or "").strip()

    if not query:
        raise SearchParamError("query is required")

    try:
        top_k = int(top_k) if top_k not in (None, "") else 5
    except (TypeError, ValueError) as exc:
        raise SearchParamError(
            "top_k must be an integer"
        ) from exc

    top_k = max(1, min(top_k, 20))

    source_type = source_type or None

    if source_type not in _ALLOWED_SOURCE_TYPES:
        raise SearchParamError(
            "source_type must be alert or forecast"
        )

    location = str(location or "").strip() or None

    return query, top_k, source_type, location


def _search_weather_chunks(
    query: str,
    top_k: int,
    source_type: str | None,
    location: str | None,
) -> list[dict]:
    """Run pgvector cosine-similarity retrieval."""
    vector = get_embedding_model().encode(
        query,
        normalize_embeddings=True,
    ).tolist()

    vector_literal = lakebase.vector_literal(vector)

    clauses = ["e.model_name = %s"]
    params: list[object] = [
        vector_literal,
        MODEL_NAME,
    ]

    if source_type:
        clauses.append("d.source_type = %s")
        params.append(source_type)

    if location:
        clauses.append("d.location = %s")
        params.append(location)

    params.extend(
        [
            vector_literal,
            top_k,
        ]
    )

    where_sql = "\n          AND ".join(clauses)

    return lakebase.run_query(
        f"""
        SELECT
            d.id,
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


def _generate_summary(
    query: str,
    rows: list[dict],
    requested: bool,
) -> tuple[str | None, str, str | None]:
    """Generate a summary and return its UI status.

    Status values:
    - not_requested
    - no_results
    - unavailable
    - generated
    - failed
    """
    if not requested:
        return None, "not_requested", None

    if not rows:
        return (
            None,
            "no_results",
            "No AI summary was generated because no search results were found.",
        )

    if not summary_is_configured():
        return (
            None,
            "unavailable",
            (
                "AI summary is not configured. Set SUMMARY_MODEL_ENDPOINT "
                "to a valid Databricks Model Serving endpoint and grant the "
                "app service principal Can Query permission."
            ),
        )

    summary = summarize_search_results(query, rows)

    if summary:
        return summary, "generated", None

    return (
        None,
        "failed",
        (
            "The vector search completed, but the AI summary request failed. "
            "Check the Model Serving endpoint name, endpoint status, app "
            "permissions and Databricks App logs."
        ),
    )


def _search_response(
    query: str,
    top_k: int,
    source_type: str | None,
    location: str | None,
    rows: list[dict],
    summary: str | None,
    summary_requested: bool,
    summary_status: str,
    summary_message: str | None,
) -> dict:
    """Shape the JSON response returned by both search routes."""
    return {
        "query": query,
        "top_k": top_k,
        "source_type": source_type,
        "location": location,
        "count": len(rows),
        "results": rows,
        "summary": summary,
        "summary_requested": summary_requested,
        "summary_status": summary_status,
        "summary_message": summary_message,
        "message": (
            None
            if rows
            else (
                "No matching embeddings were found. Sync weather "
                "documents and run the embedding notebook before searching."
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
    """Return locally supported US city/state suggestions."""
    options = []
    seen: set[str] = set()

    for key, coordinates in sorted(
        _KNOWN_US_PLACES.items()
    ):
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
    """Summarise locations stored in weather_documents."""
    rows = lakebase.run_query(
        f"""
        SELECT
            location,
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
        ORDER BY
            MAX(synced_at) DESC,
            location ASC
        """
    )

    return jsonify(rows)


@app.delete("/weather/locations/<path:location>")
def delete_weather_location(location: str):
    """Delete a location's documents and search embeddings.

    weather_embeddings.document_id has ON DELETE CASCADE.
    Deleting the weather_documents rows therefore also removes all
    vector-search chunks linked to those documents.
    """
    location = str(location or "").strip()

    if not location:
        return jsonify(
            {"error": "location is required"}
        ), 400

    with lakebase.get_connection() as connection:
        with connection.cursor() as cursor:
            # Count embeddings before deleting the parent documents.
            cursor.execute(
                f"""
                SELECT COUNT(*)::int AS embedding_count
                FROM {EMBEDDINGS_TABLE} e
                JOIN {DOCUMENTS_TABLE} d
                  ON d.id = e.document_id
                WHERE d.location = %s
                """,
                (location,),
            )

            count_row = cursor.fetchone() or {}
            deleted_embedding_count = int(
                count_row.get("embedding_count", 0)
            )

            # Related weather_embeddings rows are removed by cascade.
            cursor.execute(
                f"""
                DELETE FROM {DOCUMENTS_TABLE}
                WHERE location = %s
                RETURNING id
                """,
                (location,),
            )

            deleted_documents = list(
                cursor.fetchall()
            )

            if not deleted_documents:
                connection.rollback()

                return jsonify(
                    {
                        "error": (
                            "No stored weather documents were found for "
                            f"{location}."
                        )
                    }
                ), 404

            connection.commit()

    return jsonify(
        {
            "deleted": True,
            "location": location,
            "deleted_documents": len(
                deleted_documents
            ),
            "deleted_embeddings": (
                deleted_embedding_count
            ),
            "message": (
                f"Deleted {len(deleted_documents)} weather documents "
                f"and {deleted_embedding_count} vector-search records "
                f"for {location}."
            ),
        }
    )


@app.post("/weather/sync")
def weather_sync():
    """Harvest NWS alerts and forecasts into Lakebase.

    Required Homework 2 request:

    {
        "locations": ["Chicago, IL", "Austin, TX"],
        "limit": 50
    }
    """
    body = request.get_json(silent=True) or {}

    try:
        result = run_weather_sync(
            body.get("locations"),
            body.get("limit", 50),
        )
    except ValueError as exc:
        return jsonify(
            {"error": str(exc)}
        ), 400

    return jsonify(result)


@app.get("/weather/documents")
def weather_documents():
    """Inspect recently synchronised weather documents."""
    try:
        limit = int(
            request.args.get("limit", 50)
        )
    except (TypeError, ValueError):
        return jsonify(
            {"error": "limit must be an integer"}
        ), 400

    limit = max(1, min(limit, 200))

    rows = lakebase.run_query(
        f"""
        SELECT
            id,
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
    """Embed a query and rank weather chunks by similarity.

    Required Homework 2 request:

    {
        "query": "risk of flooding near rivers",
        "top_k": 5
    }

    Optional UI fields:

    {
        "source_type": "alert",
        "location": "New York, NY",
        "summarize": true
    }
    """
    body = request.get_json(silent=True) or {}

    try:
        (
            query,
            top_k,
            source_type,
            location,
        ) = _parse_search_params(
            body.get("query"),
            body.get("top_k", 5),
            body.get("source_type"),
            body.get("location"),
        )
    except SearchParamError as exc:
        return jsonify(
            {"error": str(exc)}
        ), 400

    rows = _search_weather_chunks(
        query=query,
        top_k=top_k,
        source_type=source_type,
        location=location,
    )

    summary_requested = _parse_boolean(
        body.get("summarize"),
        default=False,
    )

    (
        summary,
        summary_status,
        summary_message,
    ) = _generate_summary(
        query=query,
        rows=rows,
        requested=summary_requested,
    )

    return jsonify(
        _search_response(
            query=query,
            top_k=top_k,
            source_type=source_type,
            location=location,
            rows=rows,
            summary=summary,
            summary_requested=summary_requested,
            summary_status=summary_status,
            summary_message=summary_message,
        )
    )


@app.get("/weather/search")
def weather_search_get():
    """Browser-friendly variant of weather semantic search."""
    args = request.args

    try:
        (
            query,
            top_k,
            source_type,
            location,
        ) = _parse_search_params(
            args.get("query"),
            args.get("top_k", 5),
            args.get("source_type"),
            args.get("location"),
        )
    except SearchParamError as exc:
        return jsonify(
            {"error": str(exc)}
        ), 400

    summary_requested = _parse_boolean(
        args.get("summarize"),
        default=False,
    )

    rows = _search_weather_chunks(
        query=query,
        top_k=top_k,
        source_type=source_type,
        location=location,
    )

    (
        summary,
        summary_status,
        summary_message,
    ) = _generate_summary(
        query=query,
        rows=rows,
        requested=summary_requested,
    )

    return jsonify(
        _search_response(
            query=query,
            top_k=top_k,
            source_type=source_type,
            location=location,
            rows=rows,
            summary=summary,
            summary_requested=summary_requested,
            summary_status=summary_status,
            summary_message=summary_message,
        )
    )


@app.errorhandler(Exception)
def handle_exception(error):
    logger.exception(
        "Unhandled application error"
    )

    status = getattr(error, "code", 500)

    if not isinstance(status, int):
        status = 500

    return jsonify(
        {"error": str(error)}
    ), status


if __name__ == "__main__":
    app.run(
        host=os.environ.get(
            "FLASK_RUN_HOST",
            "0.0.0.0",
        ),
        port=int(
            os.environ.get(
                "FLASK_RUN_PORT",
                "8000",
            )
        ),
        debug=(
            os.environ.get(
                "FLASK_DEBUG",
                "false",
            ).lower()
            == "true"
        ),
    )