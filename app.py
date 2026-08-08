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
from weather_sync import run_weather_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nws-weather-intelligence")

app = Flask(__name__)

DOCUMENTS_TABLE = os.environ.get("WEATHER_DOCUMENTS_TABLE_NAME", "weather_documents")
EMBEDDINGS_TABLE = os.environ.get("WEATHER_EMBEDDINGS_TABLE_NAME", "weather_embeddings")
MODEL_NAME = os.environ.get("WEATHER_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# locations/limit defaults for /weather/sync live in weather_sync.py (shared
# with the scheduled job) - nothing to duplicate here.

_ALLOWED_SOURCE_TYPES = (None, "alert", "forecast")

_embedding_model: SentenceTransformer | None = None


def get_embedding_model() -> SentenceTransformer:
    """Load the 384-dimensional model once and reuse it for all searches."""
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading embedding model: %s", MODEL_NAME)
        _embedding_model = SentenceTransformer(MODEL_NAME)
    return _embedding_model


class SearchParamError(ValueError):
    """Invalid /weather/search input; str(exc) is safe to return to the client."""


def _parse_search_params(query: Any, top_k: Any, source_type: Any) -> tuple[str, int, str | None]:
    """Validate + normalize search params. Shared by the POST (JSON body) and
    GET (query string) routes so the two don't drift out of sync."""
    query = str(query or "").strip()
    if not query:
        raise SearchParamError("query is required")

    try:
        top_k = int(top_k) if top_k not in (None, "") else 5
    except (TypeError, ValueError):
        raise SearchParamError("top_k must be an integer")
    top_k = max(1, min(top_k, 20))

    source_type = source_type or None
    if source_type not in _ALLOWED_SOURCE_TYPES:
        raise SearchParamError("source_type must be alert or forecast")

    return query, top_k, source_type


def _search_weather_chunks(query: str, top_k: int, source_type: str | None) -> list[dict]:
    """Core pgvector cosine-similarity retrieval. Shared by the POST and GET
    /weather/search routes so the SQL only exists in one place."""
    vector = get_embedding_model().encode(query).tolist()
    vector_literal = lakebase.vector_literal(vector)

    source_clause = "AND d.source_type = %s" if source_type else ""
    params: list[object] = [vector_literal, MODEL_NAME]
    if source_type:
        params.append(source_type)
    params.extend([vector_literal, top_k])

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
        JOIN {DOCUMENTS_TABLE} d ON d.id = e.document_id
        WHERE e.model_name = %s
          {source_clause}
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
        """,
        tuple(params),
    )


def _search_response(query: str, top_k: int, rows: list[dict], summary: str | None) -> dict:
    """Shape the JSON body returned by both /weather/search routes."""
    return {
        "query": query,
        "top_k": top_k,
        "count": len(rows),
        "results": rows,
        "summary": summary,
        "message": (
            None
            if rows
            else "No embeddings found. Run /weather/sync and then the embedding notebook."
        ),
    }


@app.get("/")
def index():
    """Serve the main HTML interface."""
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.post("/weather/sync")
def weather_sync():
    """Harvest NWS alerts and forecasts and upsert them into Lakebase.

    Body: {"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}
    Same logic runs on a schedule via jobs/scheduled_weather_sync.py.
    """
    body = request.get_json(silent=True) or {}
    try:
        result = run_weather_sync(body.get("locations"), body.get("limit", 50))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


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
    """Embed a query and rank weather chunks with cosine similarity.

    Body: {"query": "...", "top_k": 5, "source_type": "alert"|"forecast",
           "summarize": false}
    `summarize` defaults to false here (unlike the GET variant below) so the
    default JSON contract stays exactly what the assignment specified -
    pass it explicitly to also get an LLM-generated summary.
    """
    body = request.get_json(silent=True) or {}
    try:
        query, top_k, source_type = _parse_search_params(
            body.get("query"), body.get("top_k", 5), body.get("source_type")
        )
    except SearchParamError as exc:
        return jsonify({"error": str(exc)}), 400

    rows = _search_weather_chunks(query, top_k, source_type)
    summary = summarize_search_results(query, rows) if body.get("summarize") else None
    return jsonify(_search_response(query, top_k, rows, summary))


@app.get("/weather/search")
def weather_search_get():
    """GET variant of /weather/search for simple browser/curl use, with an
    LLM-generated natural-language summary of the top results by default
    (basic RAG - see llm_summary.py).

    GET /weather/search?query=flash+flood+risk&top_k=5&source_type=alert&summarize=false
    Pass summarize=false to skip the LLM call and just get raw vector-search
    results back faster.
    """
    args = request.args
    try:
        query, top_k, source_type = _parse_search_params(
            args.get("query"), args.get("top_k", 5), args.get("source_type")
        )
    except SearchParamError as exc:
        return jsonify({"error": str(exc)}), 400

    summarize = args.get("summarize", "true").strip().lower() not in ("false", "0", "no")

    rows = _search_weather_chunks(query, top_k, source_type)
    summary = summarize_search_results(query, rows) if summarize else None
    return jsonify(_search_response(query, top_k, rows, summary))


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