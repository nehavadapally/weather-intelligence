"""Shared harvest and upsert logic for weather documents."""

from __future__ import annotations

import json
import os
from typing import Any

from psycopg2.extras import execute_values

import lakebase
from weather_client import NWSWeatherClient, WeatherClientError

DOCUMENTS_TABLE = os.environ.get(
    "WEATHER_DOCUMENTS_TABLE_NAME",
    "weather_documents",
)

DEFAULT_LOCATIONS = [
    value.strip()
    for value in os.environ.get(
        "WEATHER_LOCATIONS",
        "Chicago, IL;Austin, TX",
    ).split(";")
    if value.strip()
]


def _upsert_weather_documents(documents: list[dict[str, Any]]) -> int:
    """Upsert documents and return the number inserted or updated."""
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
            written_rows = execute_values(
                cur,
                f"""
                INSERT INTO {DOCUMENTS_TABLE} (
                    id,
                    location,
                    latitude,
                    longitude,
                    source_type,
                    headline,
                    narrative_text,
                    issued_at,
                    effective_at,
                    payload,
                    synced_at
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
                WHERE
                    {DOCUMENTS_TABLE}.narrative_text
                        IS DISTINCT FROM EXCLUDED.narrative_text
                    OR {DOCUMENTS_TABLE}.headline
                        IS DISTINCT FROM EXCLUDED.headline
                    OR {DOCUMENTS_TABLE}.payload
                        IS DISTINCT FROM EXCLUDED.payload
                RETURNING id
                """,
                rows,
                template=(
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::jsonb, now())"
                ),
                page_size=100,
                fetch=True,
            )
            conn.commit()

    return len(written_rows)


def run_weather_sync(
    locations: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Harvest NWS alerts and forecasts and upsert them into Lakebase."""
    locations = locations if locations else DEFAULT_LOCATIONS

    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc

    limit = max(1, min(limit, 200))

    if not isinstance(locations, list) or not locations:
        raise ValueError("locations must be a non-empty list")

    client = NWSWeatherClient()
    documents_by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []

    for location in locations:
        if not isinstance(location, str) or not location.strip():
            errors.append(
                {
                    "location": str(location),
                    "error": "Invalid location",
                }
            )
            continue

        try:
            documents = client.fetch_documents(
                location,
                limit=limit,
            )
            for document in documents:
                documents_by_id[document["id"]] = document
        except WeatherClientError as exc:
            errors.append(
                {
                    "location": location,
                    "error": str(exc),
                }
            )

    documents = list(documents_by_id.values())
    written = _upsert_weather_documents(documents)

    return {
        "synced": written,
        "unique_documents": len(documents),
        "locations": locations,
        "errors": errors,
    }
