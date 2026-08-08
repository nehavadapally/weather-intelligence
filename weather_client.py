"""National Weather Service API client and weather-document normaliser.

Inputs can be US place names such as ``Chicago, IL`` or coordinate strings such
as ``41.8781,-87.6298``. Coordinates are resolved with no network call at all.
Place names are resolved in two stages: first against a built-in table of all
50 state capitals plus the largest US metros (_KNOWN_US_PLACES, also no
network call), then - only for names not in that table - against the public
Nominatim geocoder.

The built-in table exists because the public nominatim.openstreetmap.org
server actively rate-limits/IP-blocks shared cloud egress ranges (the kind
Databricks Apps, AWS Lambda, CI runners, etc. all use) per its own usage
policy - see https://operations.osmfoundation.org/policies/nominatim/ - and
that block is independent of sending a valid User-Agent. Resolving the
common case locally means a `weather_resync_job` or Databricks App instance
that's already hit that block can still sync every major US city without
ever calling Nominatim.

Once a location is resolved (from either path), NWS `/points` gives the
forecast grid before active alerts and narrative forecasts are collected.
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

# All 50 US state capitals, plus the ~40 largest US metros not already
# covered by a capital, keyed by casefolded "city, st". Coordinates are
# city-center-accurate (a few hundredths of a degree), which is more than
# enough precision for NWS's 2.5km forecast grid. Add more entries here any
# time a location you need keeps falling through to (and getting blocked
# by) Nominatim - it's a plain dict, no network call required to extend it.
_KNOWN_US_PLACES: dict[str, tuple[float, float]] = {
    # --- state capitals ---
    "montgomery, al": (32.3792, -86.3077),
    "juneau, ak": (58.3019, -134.4197),
    "phoenix, az": (33.4484, -112.0740),
    "little rock, ar": (34.7465, -92.2896),
    "sacramento, ca": (38.5816, -121.4944),
    "denver, co": (39.7392, -104.9903),
    "hartford, ct": (41.7658, -72.6734),
    "dover, de": (39.1582, -75.5244),
    "tallahassee, fl": (30.4383, -84.2807),
    "atlanta, ga": (33.7490, -84.3880),
    "honolulu, hi": (21.3069, -157.8583),
    "boise, id": (43.6150, -116.2023),
    "springfield, il": (39.7817, -89.6501),
    "indianapolis, in": (39.7684, -86.1581),
    "des moines, ia": (41.5868, -93.6250),
    "topeka, ks": (39.0473, -95.6752),
    "frankfort, ky": (38.2009, -84.8733),
    "baton rouge, la": (30.4515, -91.1871),
    "augusta, me": (44.3106, -69.7795),
    "annapolis, md": (38.9784, -76.4922),
    "boston, ma": (42.3601, -71.0589),
    "lansing, mi": (42.7325, -84.5555),
    "saint paul, mn": (44.9537, -93.0900),
    "st. paul, mn": (44.9537, -93.0900),
    "jackson, ms": (32.2988, -90.1848),
    "jefferson city, mo": (38.5767, -92.1735),
    "helena, mt": (46.5891, -112.0391),
    "lincoln, ne": (40.8136, -96.7026),
    "carson city, nv": (39.1638, -119.7674),
    "concord, nh": (43.2081, -71.5376),
    "trenton, nj": (40.2171, -74.7429),
    "santa fe, nm": (35.6870, -105.9378),
    "albany, ny": (42.6526, -73.7562),
    "raleigh, nc": (35.7796, -78.6382),
    "bismarck, nd": (46.8083, -100.7837),
    "columbus, oh": (39.9612, -82.9988),
    "oklahoma city, ok": (35.4676, -97.5164),
    "salem, or": (44.9429, -123.0351),
    "harrisburg, pa": (40.2732, -76.8867),
    "providence, ri": (41.8240, -71.4128),
    "columbia, sc": (34.0007, -81.0348),
    "pierre, sd": (44.3683, -100.3510),
    "nashville, tn": (36.1627, -86.7816),
    "austin, tx": (30.2672, -97.7431),
    "salt lake city, ut": (40.7608, -111.8910),
    "montpelier, vt": (44.2601, -72.5754),
    "richmond, va": (37.5407, -77.4360),
    "olympia, wa": (47.0379, -122.9007),
    "charleston, wv": (38.3498, -81.6326),
    "madison, wi": (43.0731, -89.4012),
    "cheyenne, wy": (41.1400, -104.8202),
    # --- largest metros not already covered above ---
    "new york, ny": (40.7128, -74.0060),
    "los angeles, ca": (34.0522, -118.2437),
    "chicago, il": (41.8781, -87.6298),
    "houston, tx": (29.7604, -95.3698),
    "san antonio, tx": (29.4241, -98.4936),
    "san diego, ca": (32.7157, -117.1611),
    "dallas, tx": (32.7767, -96.7970),
    "san jose, ca": (37.3382, -121.8863),
    "fort worth, tx": (32.7555, -97.3308),
    "jacksonville, fl": (30.3322, -81.6557),
    "charlotte, nc": (35.2271, -80.8431),
    "san francisco, ca": (37.7749, -122.4194),
    "seattle, wa": (47.6062, -122.3321),
    "detroit, mi": (42.3314, -83.0458),
    "portland, or": (45.5152, -122.6784),
    "memphis, tn": (35.1495, -90.0490),
    "louisville, ky": (38.2527, -85.7585),
    "milwaukee, wi": (43.0389, -87.9065),
    "albuquerque, nm": (35.0844, -106.6504),
    "tucson, az": (32.2226, -110.9747),
    "fresno, ca": (36.7378, -119.7871),
    "mesa, az": (33.4152, -111.8315),
    "kansas city, mo": (39.0997, -94.5786),
    "omaha, ne": (41.2565, -95.9345),
    "colorado springs, co": (38.8339, -104.8214),
    "miami, fl": (25.7617, -80.1918),
    "long beach, ca": (33.7701, -118.1937),
    "virginia beach, va": (36.8529, -75.9780),
    "oakland, ca": (37.8044, -122.2712),
    "minneapolis, mn": (44.9778, -93.2650),
    "tulsa, ok": (36.1540, -95.9928),
    "tampa, fl": (27.9506, -82.4572),
    "arlington, tx": (32.7357, -97.1081),
    "new orleans, la": (29.9511, -90.0715),
}


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
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 403 and url.startswith(self.geocoder_base_url):
                raise WeatherClientError(
                    f"Geocoder request blocked (403) for {url}. This is "
                    "Nominatim's own anti-abuse IP block, not a bug in this "
                    "client - the public nominatim.openstreetmap.org server "
                    "blocks shared cloud/PaaS egress IPs (Databricks Apps, "
                    "AWS Lambda, CI runners, etc.) per its usage policy, "
                    "independent of the User-Agent sent. This location isn't "
                    "in the built-in _KNOWN_US_PLACES table (checked first, "
                    "no network call) - add it there to resolve it without "
                    "ever calling Nominatim, or point GEOCODER_BASE_URL at "
                    "your own Nominatim instance. See "
                    "https://operations.osmfoundation.org/policies/nominatim/."
                ) from exc
            raise WeatherClientError(f"Request failed for {url}: {exc}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise WeatherClientError(f"Request failed for {url}: {exc}") from exc

    @staticmethod
    def _validate_coordinates(latitude: float, longitude: float) -> None:
        if not -90 <= latitude <= 90:
            raise WeatherClientError(f"Invalid latitude: {latitude}")
        if not -180 <= longitude <= 180:
            raise WeatherClientError(f"Invalid longitude: {longitude}")

    def resolve_location(self, location: str) -> ResolvedLocation:
        """Resolve a US city/state, a "lat,lon" pair, or a known place name.

        Resolution order:
        1. "lat,lon" - parsed directly, no network call.
        2. _KNOWN_US_PLACES (state capitals + largest metros) - no network
           call, and immune to Nominatim's IP-based blocking (see _get()).
        3. Nominatim/OpenStreetMap geocoding - only reached for names not
           covered by the first two, and may occasionally 403 from a shared
           cloud IP; _get() turns that into an actionable error rather than
           a raw exception.
        """
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

        if cache_key in _KNOWN_US_PLACES:
            latitude, longitude = _KNOWN_US_PLACES[cache_key]
            resolved = ResolvedLocation(
                requested=requested, label=requested, latitude=latitude, longitude=longitude
            )
            self._geocode_cache[cache_key] = resolved
            return resolved

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
            raise WeatherClientError(
                f"Could not resolve location: {requested!r}. Expected a "
                '"City, ST" name (e.g. "New York, NY") or "lat,lon" '
                'coordinates (e.g. "40.7128,-74.0060").'
            )

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