"""National Weather Service API client and weather-document normaliser.

Inputs can be US place names such as ``Chicago, IL`` or coordinate strings such
as ``41.8781,-87.6298``. Place names are resolved through the public Nominatim
geocoder. Coordinates are then resolved through NWS ``/points`` before active
alerts and narrative forecasts are collected.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

NWS_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov").rstrip("/")
GEOCODER_BASE_URL = os.environ.get(
    "GEOCODER_BASE_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
NWS_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "weather-intelligence-homework/1.0 (replace-with-your-email@example.com)",
)
HTTP_TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "30"))

_COORDINATE_PATTERN = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)


class WeatherClientError(RuntimeError):
    """Raised when a location or weather API request cannot be processed."""


@dataclass(frozen=True)
class ResolvedLocation:
    requested: str
    label: str
    latitude: float
    longitude: float


def clean_text(value: Any) -> str:
    """Collapse repeated whitespace while preserving readable text."""
    return " ".join(str(value or "").split()).strip()


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class NWSWeatherClient:
    """Thin requests-based client for geocoding and NWS weather endpoints."""

    def __init__(
        self,
        nws_base_url: str | None = None,
        geocoder_base_url: str | None = None,
        user_agent: str | None = None,
        timeout: int = HTTP_TIMEOUT,
    ) -> None:
        self.nws_base_url = (nws_base_url or NWS_BASE_URL).rstrip("/")
        self.geocoder_base_url = (geocoder_base_url or GEOCODER_BASE_URL).rstrip("/")
        self.user_agent = user_agent or NWS_USER_AGENT
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/geo+json, application/json",
                "User-Agent": self.user_agent,
            }
        )
        self._geocode_cache: dict[str, ResolvedLocation] = {}
        self._last_geocode_at = 0.0

    def _get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WeatherClientError(f"Request failed for {url}: {exc}") from exc

    @staticmethod
    def _validate_coordinates(latitude: float, longitude: float) -> None:
        if not -90 <= latitude <= 90:
            raise WeatherClientError(f"Invalid latitude: {latitude}")
        if not -180 <= longitude <= 180:
            raise WeatherClientError(f"Invalid longitude: {longitude}")

    def resolve_location(self, location: str) -> ResolvedLocation:
        """Resolve a US city/state or latitude/longitude pair."""
        requested = clean_text(location)
        if not requested:
            raise WeatherClientError("Location must be a non-empty string")

        match = _COORDINATE_PATTERN.match(requested)
        if match:
            latitude = float(match.group(1))
            longitude = float(match.group(2))
            self._validate_coordinates(latitude, longitude)
            return ResolvedLocation(requested, requested, latitude, longitude)

        cache_key = requested.casefold()
        if cache_key in self._geocode_cache:
            return self._geocode_cache[cache_key]

        elapsed = time.monotonic() - self._last_geocode_at
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        results = self._get(
            f"{self.geocoder_base_url}/search",
            params={
                "q": requested,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us",
                "addressdetails": 1,
            },
        )
        self._last_geocode_at = time.monotonic()

        if not isinstance(results, list) or not results:
            raise WeatherClientError(f"Could not resolve location: {requested}")

        result = results[0]
        try:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherClientError(
                f"Geocoder returned invalid coordinates for {requested}"
            ) from exc

        self._validate_coordinates(latitude, longitude)
        resolved = ResolvedLocation(
            requested=requested,
            label=clean_text(result.get("display_name")) or requested,
            latitude=latitude,
            longitude=longitude,
        )
        self._geocode_cache[cache_key] = resolved
        return resolved

    def get_point_metadata(self, latitude: float, longitude: float) -> dict[str, Any]:
        self._validate_coordinates(latitude, longitude)
        payload = self._get(
            f"{self.nws_base_url}/points/{latitude:.4f},{longitude:.4f}"
        )
        if not isinstance(payload, dict):
            raise WeatherClientError("NWS /points returned an unexpected response")
        return payload

    def get_active_alerts(
        self, latitude: float, longitude: float
    ) -> list[dict[str, Any]]:
        payload = self._get(
            f"{self.nws_base_url}/alerts/active",
            params={"point": f"{latitude:.4f},{longitude:.4f}"},
        )
        if not isinstance(payload, dict):
            return []
        features = payload.get("features") or []
        return [item for item in features if isinstance(item, dict)]

    def get_forecast(self, point_metadata: dict[str, Any]) -> dict[str, Any]:
        forecast_url = clean_text((point_metadata.get("properties") or {}).get("forecast"))
        if not forecast_url:
            raise WeatherClientError("NWS /points response did not include forecast URL")
        payload = self._get(forecast_url)
        if not isinstance(payload, dict):
            raise WeatherClientError("NWS forecast returned an unexpected response")
        return payload

    def fetch_documents(self, location: str, limit: int = 50) -> list[dict[str, Any]]:
        """Collect and normalise active alerts and forecast periods."""
        limit = max(1, min(int(limit), 200))
        resolved = self.resolve_location(location)
        point_metadata = self.get_point_metadata(resolved.latitude, resolved.longitude)
        alerts = self.get_active_alerts(resolved.latitude, resolved.longitude)
        forecast = self.get_forecast(point_metadata)

        documents: list[dict[str, Any]] = []
        for feature in alerts:
            document = self._normalise_alert(feature, resolved)
            if document:
                documents.append(document)

        periods = (forecast.get("properties") or {}).get("periods") or []
        for period in periods:
            if not isinstance(period, dict):
                continue
            document = self._normalise_forecast(period, forecast, resolved)
            if document:
                documents.append(document)

        # Alerts appear first because urgent narratives should not be displaced
        # by forecast periods when a small limit is requested.
        return documents[:limit]

    def _normalise_alert(
        self, feature: dict[str, Any], location: ResolvedLocation
    ) -> dict[str, Any] | None:
        properties = feature.get("properties") or {}
        description = clean_text(properties.get("description"))
        instruction = clean_text(properties.get("instruction"))
        narrative_text = "\n\n".join(
            part for part in (description, instruction) if part
        )
        if not narrative_text:
            return None

        source_id = clean_text(feature.get("id") or properties.get("id"))
        if not source_id:
            source_id = stable_hash(
                location.label,
                clean_text(properties.get("event")),
                clean_text(properties.get("sent") or properties.get("effective")),
                narrative_text,
            )

        headline = clean_text(
            properties.get("headline")
            or properties.get("event")
            or "NWS weather alert"
        )

        return {
            "id": f"nws-alert:{source_id}",
            "location": location.label,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "source_type": "alert",
            "headline": headline,
            "narrative_text": narrative_text,
            "issued_at": properties.get("sent") or properties.get("onset"),
            "effective_at": properties.get("effective") or properties.get("onset"),
            "payload": feature,
        }

    def _normalise_forecast(
        self,
        period: dict[str, Any],
        forecast: dict[str, Any],
        location: ResolvedLocation,
    ) -> dict[str, Any] | None:
        detailed_forecast = clean_text(period.get("detailedForecast"))
        if not detailed_forecast:
            return None

        period_name = clean_text(period.get("name")) or "Forecast"
        start_time = clean_text(period.get("startTime"))
        forecast_id = stable_hash(
            f"{location.latitude:.4f}",
            f"{location.longitude:.4f}",
            start_time,
            period_name,
        )
        generated_at = (forecast.get("properties") or {}).get("generatedAt")

        return {
            "id": f"nws-forecast:{forecast_id}",
            "location": location.label,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "source_type": "forecast",
            "headline": period_name,
            "narrative_text": detailed_forecast,
            "issued_at": generated_at or forecast.get("updated"),
            "effective_at": period.get("startTime"),
            "payload": period,
        }
