"""Environment Agency flood-warning client for Homework 2.

The Environment Agency Real-Time Flood Monitoring API is a free, unauthenticated
JSON API for England. This client treats flood warnings as unstructured weather
alerts and normalises their narrative messages into the document schema used by
Lakebase and pgvector.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import requests

_BASE_URL = os.environ.get(
    "EA_FLOOD_API_BASE_URL",
    "https://environment.data.gov.uk/flood-monitoring",
).rstrip("/")
_USER_AGENT = os.environ.get(
    "WEATHER_USER_AGENT",
    "databricks-weather-intelligence-homework/1.0",
)
_DEFAULT_TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "30"))
_COORDINATE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)


class WeatherClientError(RuntimeError):
    """Raised when a flood-monitoring request or response is invalid."""


class EnvironmentAgencyWeatherClient:
    """Fetch and normalise Environment Agency flood warnings.

    A location selector can be:

    * a county/area string, for example ``"Somerset"``;
    * coordinates, for example ``"51.5074,-0.1278"``;
    * ``"all"`` to request warnings across England.
    """

    attribution = (
        "This uses Environment Agency flood and river level data from the "
        "real-time data API (Beta)."
    )

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent or _USER_AGENT
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            }
        )

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.get(
                url,
                params=params,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WeatherClientError(f"Environment Agency request failed: {exc}") from exc

        if not isinstance(payload, dict):
            raise WeatherClientError("Environment Agency returned an unexpected response")
        return payload

    @staticmethod
    def _validate_coordinates(latitude: float, longitude: float) -> None:
        if not -90 <= latitude <= 90:
            raise WeatherClientError(f"Invalid latitude: {latitude}")
        if not -180 <= longitude <= 180:
            raise WeatherClientError(f"Invalid longitude: {longitude}")

    @staticmethod
    def _bounded_int(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(int(value), maximum))

    def fetch_documents(
        self,
        location: str = "all",
        *,
        limit: int = 50,
        radius_km: int = 50,
        min_severity: int = 3,
    ) -> list[dict[str, Any]]:
        """Fetch warnings for one selector and return normalised documents.

        Environment Agency severity levels are:
        1 = Severe Flood Warning, 2 = Flood Warning, 3 = Flood Alert,
        4 = Warning no longer in force. ``min_severity=3`` therefore returns
        current levels 1-3; use 4 when a classroom demo needs a wider sample.
        """
        if not isinstance(location, str) or not location.strip():
            raise WeatherClientError("Location must be a non-empty string")

        selector = location.strip()
        limit = self._bounded_int(limit, 1, 500)
        radius_km = self._bounded_int(radius_km, 1, 200)
        min_severity = self._bounded_int(min_severity, 1, 4)

        params: dict[str, Any] = {
            "_limit": limit,
            "min-severity": min_severity,
        }
        query_latitude: float | None = None
        query_longitude: float | None = None

        coordinate_match = _COORDINATE_RE.match(selector)
        if coordinate_match:
            query_latitude = float(coordinate_match.group(1))
            query_longitude = float(coordinate_match.group(2))
            self._validate_coordinates(query_latitude, query_longitude)
            params.update(
                {
                    "lat": query_latitude,
                    "long": query_longitude,
                    "dist": radius_km,
                }
            )
        elif selector.casefold() != "all":
            params["county"] = selector

        payload = self._get("/id/floods", params)
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise WeatherClientError("Environment Agency response has no valid items list")

        documents: list[dict[str, Any]] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            document = self._normalise_warning(
                item,
                requested_location=selector,
                query_latitude=query_latitude,
                query_longitude=query_longitude,
            )
            if document:
                documents.append(document)
        return documents

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    @staticmethod
    def _stable_hash(*parts: str) -> str:
        joined = "|".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _normalise_warning(
        self,
        item: dict[str, Any],
        *,
        requested_location: str,
        query_latitude: float | None,
        query_longitude: float | None,
    ) -> dict[str, Any] | None:
        flood_area_id = self._clean_text(item.get("floodAreaID"))
        source_url = self._clean_text(item.get("@id"))
        description = self._clean_text(item.get("description"))
        message = self._clean_text(item.get("message"))
        severity = self._clean_text(item.get("severity")) or "Flood warning"
        flood_area = item.get("floodArea") or {}
        if not isinstance(flood_area, dict):
            flood_area = {}
        county = self._clean_text(flood_area.get("county") or item.get("county"))
        river_or_sea = self._clean_text(flood_area.get("riverOrSea"))
        ea_area_name = self._clean_text(item.get("eaAreaName"))
        ea_region_name = self._clean_text(item.get("eaRegionName"))

        try:
            severity_level = int(item.get("severityLevel"))
        except (TypeError, ValueError):
            severity_level = None

        narrative_parts = [
            f"Severity: {severity}.",
            f"Flood area: {description}." if description else "",
            f"County: {county}." if county else "",
            f"River or sea: {river_or_sea}." if river_or_sea else "",
            f"Environment Agency area: {ea_area_name}." if ea_area_name else "",
            f"Region: {ea_region_name}." if ea_region_name else "",
            message,
        ]
        narrative_text = "\n\n".join(part for part in narrative_parts if part)
        if not narrative_text:
            return None

        source_key = flood_area_id or source_url
        if not source_key:
            source_key = self._stable_hash(
                description,
                severity,
                self._clean_text(item.get("timeRaised")),
                narrative_text,
            )

        location = description or county or requested_location or "England"
        headline = f"{severity}: {location}" if location else severity

        return {
            "id": f"ea-flood:{source_key}",
            "location": location,
            "county": county or None,
            "query_latitude": query_latitude,
            "query_longitude": query_longitude,
            "source_type": "alert",
            "headline": headline,
            "narrative_text": narrative_text,
            "severity": severity,
            "severity_level": severity_level,
            "flood_area_id": flood_area_id or None,
            "river_or_sea": river_or_sea or None,
            "ea_area_name": ea_area_name or None,
            "ea_region_name": ea_region_name or None,
            "source_url": source_url or None,
            "issued_at": item.get("timeRaised"),
            "effective_at": item.get("timeMessageChanged")
            or item.get("timeSeverityChanged"),
            "payload": item,
        }
