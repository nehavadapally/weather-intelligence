"""Adapter for National Weather Service weather data.

This module contains all HTTP calls, location resolution, response parsing,
and recommendation logic used by the MCP tools. The FastMCP server should
remain thin and delegate its work to this adapter.

The National Weather Service API is free and does not require an API key, but
it only supports locations in the United States and its territories.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

NWS_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov").rstrip("/")
GEOCODER_BASE_URL = os.environ.get(
    "GEOCODER_BASE_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
NWS_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "weather-intelligence-mcp/1.0 (replace-with-your-email@example.com)",
)
HTTP_TIMEOUT = int(os.environ.get("WEATHER_HTTP_TIMEOUT", "30"))

_COORDINATE_PATTERN = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)
_WIND_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

# Common US locations are resolved locally to avoid Nominatim rate limiting
# from shared cloud egress addresses. Coordinates are city-centre values and
# are sufficiently accurate for the NWS forecast grid.
_KNOWN_US_PLACES: dict[str, tuple[float, float]] = {
    "atlanta, ga": (33.7490, -84.3880),
    "austin, tx": (30.2672, -97.7431),
    "boston, ma": (42.3601, -71.0589),
    "charlotte, nc": (35.2271, -80.8431),
    "chicago, il": (41.8781, -87.6298),
    "dallas, tx": (32.7767, -96.7970),
    "denver, co": (39.7392, -104.9903),
    "detroit, mi": (42.3314, -83.0458),
    "houston, tx": (29.7604, -95.3698),
    "las vegas, nv": (36.1699, -115.1398),
    "los angeles, ca": (34.0522, -118.2437),
    "miami, fl": (25.7617, -80.1918),
    "minneapolis, mn": (44.9778, -93.2650),
    "nashville, tn": (36.1627, -86.7816),
    "new orleans, la": (29.9511, -90.0715),
    "new york, ny": (40.7128, -74.0060),
    "orlando, fl": (28.5383, -81.3792),
    "philadelphia, pa": (39.9526, -75.1652),
    "phoenix, az": (33.4484, -112.0740),
    "portland, or": (45.5152, -122.6784),
    "san antonio, tx": (29.4241, -98.4936),
    "san diego, ca": (32.7157, -117.1611),
    "san francisco, ca": (37.7749, -122.4194),
    "seattle, wa": (47.6062, -122.3321),
    "washington, dc": (38.9072, -77.0369),
}


class WeatherAdapterError(RuntimeError):
    """Raised when weather data cannot be resolved or retrieved safely."""


@dataclass(frozen=True)
class ResolvedLocation:
    """A user location resolved to coordinates accepted by NWS."""

    requested: str
    label: str
    latitude: float
    longitude: float


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _unit_value(measurement: Any) -> float | None:
    if not isinstance(measurement, dict):
        return None
    value = measurement.get("value")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _c_to_f(value: float | None) -> float | None:
    return None if value is None else round((value * 9 / 5) + 32, 1)


def _f_to_c(value: float | None) -> float | None:
    return None if value is None else round((value - 32) * 5 / 9, 1)


def _mps_to_mph(value: float | None) -> float | None:
    return None if value is None else round(value * 2.236936, 1)


def _forecast_wind_mps(wind_speed: str) -> float | None:
    """Convert an NWS wind string such as '5 to 10 mph' to maximum m/s."""
    numbers = [float(item) for item in _WIND_NUMBER_PATTERN.findall(wind_speed or "")]
    if not numbers:
        return None

    maximum = max(numbers)
    lowered = (wind_speed or "").lower()
    if "km/h" in lowered or "kph" in lowered:
        return round(maximum / 3.6, 1)
    if "m/s" in lowered:
        return round(maximum, 1)
    # NWS forecast wind values are normally miles per hour.
    return round(maximum / 2.236936, 1)


class NWSWeatherAdapter:
    """HTTP adapter for live NWS conditions, forecasts and alerts."""

    def __init__(
        self,
        *,
        nws_base_url: str = NWS_BASE_URL,
        geocoder_base_url: str = GEOCODER_BASE_URL,
        user_agent: str = NWS_USER_AGENT,
        timeout: int = HTTP_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.nws_base_url = nws_base_url.rstrip("/")
        self.geocoder_base_url = geocoder_base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/geo+json, application/json",
                "User-Agent": user_agent,
            }
        )
        self._geocode_cache: dict[str, ResolvedLocation] = {}
        self._last_geocode_at = 0.0

    def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 403 and url.startswith(self.geocoder_base_url):
                raise WeatherAdapterError(
                    "The public geocoder rejected the request. Use a supported "
                    "'City, ST' value from the local catalogue or supply latitude "
                    "and longitude as 'lat,lon'."
                ) from exc
            raise WeatherAdapterError(f"Weather request failed: {exc}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise WeatherAdapterError(f"Weather request failed: {exc}") from exc

    @staticmethod
    def _validate_coordinates(latitude: float, longitude: float) -> None:
        if not -90 <= latitude <= 90:
            raise WeatherAdapterError(f"Invalid latitude: {latitude}")
        if not -180 <= longitude <= 180:
            raise WeatherAdapterError(f"Invalid longitude: {longitude}")

    def resolve_location(self, location: str) -> ResolvedLocation:
        """Resolve 'City, ST' or 'lat,lon' to a US coordinate pair."""
        requested = _clean_text(location)
        if not requested:
            raise WeatherAdapterError("Location must be a non-empty string.")

        coordinate_match = _COORDINATE_PATTERN.match(requested)
        if coordinate_match:
            latitude = float(coordinate_match.group(1))
            longitude = float(coordinate_match.group(2))
            self._validate_coordinates(latitude, longitude)
            return ResolvedLocation(requested, requested, latitude, longitude)

        key = requested.casefold()
        if key in self._geocode_cache:
            return self._geocode_cache[key]

        if key in _KNOWN_US_PLACES:
            latitude, longitude = _KNOWN_US_PLACES[key]
            resolved = ResolvedLocation(requested, requested, latitude, longitude)
            self._geocode_cache[key] = resolved
            return resolved

        elapsed = time.monotonic() - self._last_geocode_at
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        results = self._get_json(
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
            raise WeatherAdapterError(
                "The location could not be resolved. Use 'City, ST' or "
                "latitude and longitude as 'lat,lon'."
            )

        first = results[0]
        try:
            latitude = float(first["lat"])
            longitude = float(first["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherAdapterError("The geocoder returned invalid coordinates.") from exc

        self._validate_coordinates(latitude, longitude)
        resolved = ResolvedLocation(
            requested=requested,
            label=_clean_text(first.get("display_name")) or requested,
            latitude=latitude,
            longitude=longitude,
        )
        self._geocode_cache[key] = resolved
        return resolved

    def _point_metadata(self, resolved: ResolvedLocation) -> dict[str, Any]:
        payload = self._get_json(
            f"{self.nws_base_url}/points/{resolved.latitude:.4f},{resolved.longitude:.4f}"
        )
        if not isinstance(payload, dict):
            raise WeatherAdapterError("NWS returned invalid point metadata.")
        return payload

    def get_current_weather(self, location: str) -> dict[str, Any]:
        """Return the latest observation from the nearest NWS station."""
        resolved = self.resolve_location(location)
        point = self._point_metadata(resolved)
        properties = point.get("properties") or {}
        stations_url = _clean_text(properties.get("observationStations"))
        if not stations_url:
            raise WeatherAdapterError("No NWS observation station was provided.")

        stations = self._get_json(stations_url)
        if not isinstance(stations, dict):
            raise WeatherAdapterError("NWS returned invalid station data.")

        features = stations.get("features") or []
        if not features:
            raise WeatherAdapterError("No nearby NWS observation station was found.")

        station = features[0]
        station_url = _clean_text(station.get("id"))
        if not station_url:
            raise WeatherAdapterError("The NWS station response was incomplete.")

        observation = self._get_json(f"{station_url}/observations/latest")
        if not isinstance(observation, dict):
            raise WeatherAdapterError("NWS returned invalid observation data.")

        observed = observation.get("properties") or {}
        temperature_c = _unit_value(observed.get("temperature"))
        wind_mps = _unit_value(observed.get("windSpeed"))
        humidity = _unit_value(observed.get("relativeHumidity"))
        wind_direction = _unit_value(observed.get("windDirection"))

        station_properties = station.get("properties") or {}
        return {
            "location": resolved.label,
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "station": station_properties.get("name") or station_url.rsplit("/", 1)[-1],
            "observed_at": observed.get("timestamp"),
            "conditions": _clean_text(observed.get("textDescription")) or "Not reported",
            "temperature_c": None if temperature_c is None else round(temperature_c, 1),
            "temperature_f": _c_to_f(temperature_c),
            "humidity_percent": None if humidity is None else round(humidity, 1),
            "wind_speed_mps": None if wind_mps is None else round(wind_mps, 1),
            "wind_speed_mph": _mps_to_mph(wind_mps),
            "wind_direction_degrees": (
                None if wind_direction is None else round(wind_direction, 1)
            ),
            "source": "National Weather Service",
        }

    def get_forecast(self, location: str, days: int = 3) -> dict[str, Any]:
        """Return NWS forecast periods for the requested number of days."""
        try:
            days = int(days)
        except (TypeError, ValueError) as exc:
            raise WeatherAdapterError("days must be an integer.") from exc
        days = max(1, min(days, 7))

        resolved = self.resolve_location(location)
        point = self._point_metadata(resolved)
        point_properties = point.get("properties") or {}
        forecast_url = _clean_text(point_properties.get("forecast"))
        if not forecast_url:
            raise WeatherAdapterError("No NWS forecast URL was provided.")

        forecast = self._get_json(forecast_url)
        if not isinstance(forecast, dict):
            raise WeatherAdapterError("NWS returned invalid forecast data.")

        forecast_properties = forecast.get("properties") or {}
        periods = forecast_properties.get("periods") or []
        cleaned_periods: list[dict[str, Any]] = []

        for period in periods[: days * 2]:
            if not isinstance(period, dict):
                continue
            temperature = period.get("temperature")
            temperature_unit = _clean_text(period.get("temperatureUnit")).upper() or "F"
            try:
                temperature_value = float(temperature)
            except (TypeError, ValueError):
                temperature_value = None

            if temperature_unit == "C":
                temperature_c = temperature_value
                temperature_f = _c_to_f(temperature_value)
            else:
                temperature_f = temperature_value
                temperature_c = _f_to_c(temperature_value)

            precipitation = _unit_value(period.get("probabilityOfPrecipitation"))
            cleaned_periods.append(
                {
                    "name": period.get("name"),
                    "start_time": period.get("startTime"),
                    "end_time": period.get("endTime"),
                    "is_daytime": bool(period.get("isDaytime")),
                    "temperature_f": (
                        None if temperature_f is None else round(temperature_f, 1)
                    ),
                    "temperature_c": (
                        None if temperature_c is None else round(temperature_c, 1)
                    ),
                    "precipitation_probability_percent": (
                        None if precipitation is None else round(precipitation, 1)
                    ),
                    "wind_speed": _clean_text(period.get("windSpeed")),
                    "wind_direction": _clean_text(period.get("windDirection")),
                    "conditions": _clean_text(period.get("shortForecast")),
                    "detailed_forecast": _clean_text(period.get("detailedForecast")),
                }
            )

        if not cleaned_periods:
            raise WeatherAdapterError("No forecast periods were returned by NWS.")

        return {
            "location": resolved.label,
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "time_zone": point_properties.get("timeZone") or "UTC",
            "generated_at": forecast_properties.get("generatedAt") or forecast.get("updated"),
            "days_requested": days,
            "periods": cleaned_periods,
            "source": "National Weather Service",
        }

    def get_active_alerts(self, location: str) -> dict[str, Any]:
        """Return active NWS alerts for a resolved location."""
        resolved = self.resolve_location(location)
        payload = self._get_json(
            f"{self.nws_base_url}/alerts/active",
            params={"point": f"{resolved.latitude:.4f},{resolved.longitude:.4f}"},
        )
        if not isinstance(payload, dict):
            raise WeatherAdapterError("NWS returned invalid alert data.")

        alerts: list[dict[str, Any]] = []
        for feature in payload.get("features") or []:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            alerts.append(
                {
                    "id": feature.get("id") or properties.get("id"),
                    "event": properties.get("event"),
                    "headline": _clean_text(properties.get("headline")),
                    "severity": properties.get("severity"),
                    "urgency": properties.get("urgency"),
                    "certainty": properties.get("certainty"),
                    "onset": properties.get("onset"),
                    "expires": properties.get("expires"),
                    "description": _clean_text(properties.get("description")),
                    "instruction": _clean_text(properties.get("instruction")),
                }
            )

        return {
            "location": resolved.label,
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "alert_count": len(alerts),
            "alerts": alerts,
            "source": "National Weather Service",
        }

    @staticmethod
    def _target_date(value: str | None, time_zone: str, periods: list[dict]) -> date:
        try:
            zone = ZoneInfo(time_zone)
        except Exception:
            zone = ZoneInfo("UTC")

        today = datetime.now(zone).date()
        normalised = _clean_text(value).lower()
        if not normalised:
            first_start = periods[0].get("start_time") if periods else None
            if first_start:
                try:
                    return datetime.fromisoformat(first_start).date()
                except (TypeError, ValueError):
                    pass
            return today
        if normalised == "today":
            return today
        if normalised == "tomorrow":
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(normalised)
        except ValueError as exc:
            raise WeatherAdapterError(
                "date must be 'today', 'tomorrow', or YYYY-MM-DD."
            ) from exc

    def get_recommendation(
        self,
        location: str,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        """Derive practical umbrella, clothing and outdoor guidance."""
        forecast = self.get_forecast(location, days=7)
        target = self._target_date(
            target_date,
            forecast["time_zone"],
            forecast["periods"],
        )

        matching_periods: list[dict[str, Any]] = []
        for period in forecast["periods"]:
            try:
                period_date = datetime.fromisoformat(period["start_time"]).date()
            except (TypeError, ValueError):
                continue
            if period_date == target:
                matching_periods.append(period)

        if not matching_periods:
            raise WeatherAdapterError(
                f"No forecast period is available for {target.isoformat()}."
            )

        precipitation_values = [
            float(item["precipitation_probability_percent"])
            for item in matching_periods
            if item.get("precipitation_probability_percent") is not None
        ]
        temperatures_c = [
            float(item["temperature_c"])
            for item in matching_periods
            if item.get("temperature_c") is not None
        ]
        wind_values = [
            value
            for value in (
                _forecast_wind_mps(item.get("wind_speed", ""))
                for item in matching_periods
            )
            if value is not None
        ]

        max_precipitation = max(precipitation_values, default=0.0)
        min_temperature = min(temperatures_c) if temperatures_c else None
        max_temperature = max(temperatures_c) if temperatures_c else None
        max_wind = max(wind_values, default=0.0)

        text = " ".join(
            f"{item.get('conditions', '')} {item.get('detailed_forecast', '')}"
            for item in matching_periods
        ).lower()
        rain_language = any(
            word in text
            for word in ("rain", "shower", "thunderstorm", "drizzle", "snow", "sleet")
        )
        severe_language = any(
            word in text
            for word in ("severe", "thunderstorm", "tornado", "blizzard", "ice storm")
        )

        alerts_payload = self.get_active_alerts(location)
        active_alerts = alerts_payload["alerts"]

        umbrella = max_precipitation >= 40 or rain_language
        jacket = (
            min_temperature is not None and min_temperature <= 15
        ) or max_wind >= 8
        heat_caution = max_temperature is not None and max_temperature >= 32

        if active_alerts or severe_language or max_precipitation >= 70 or max_wind >= 15:
            outdoor_plan = "Avoid or postpone exposed outdoor activity where practical."
        elif umbrella or max_wind >= 10 or heat_caution:
            outdoor_plan = "Outdoor plans may proceed with precautions."
        else:
            outdoor_plan = "Conditions appear generally suitable for normal outdoor plans."

        reasons: list[str] = []
        if umbrella:
            reasons.append(
                f"Maximum precipitation probability is {round(max_precipitation)}%."
            )
        else:
            reasons.append(
                f"Maximum precipitation probability is {round(max_precipitation)}%."
            )
        if min_temperature is not None:
            reasons.append(f"Minimum forecast temperature is {min_temperature:.1f}°C.")
        if max_temperature is not None:
            reasons.append(f"Maximum forecast temperature is {max_temperature:.1f}°C.")
        if max_wind:
            reasons.append(f"Maximum forecast wind is approximately {max_wind:.1f} m/s.")
        if active_alerts:
            reasons.append(f"There are {len(active_alerts)} active NWS alert(s).")

        return {
            "location": forecast["location"],
            "date": target.isoformat(),
            "recommendation": {
                "bring_umbrella": umbrella,
                "bring_jacket": jacket,
                "heat_caution": heat_caution,
                "outdoor_plan": outdoor_plan,
            },
            "evidence": {
                "maximum_precipitation_probability_percent": round(max_precipitation, 1),
                "minimum_temperature_c": min_temperature,
                "maximum_temperature_c": max_temperature,
                "maximum_wind_speed_mps": round(max_wind, 1),
                "active_alert_count": len(active_alerts),
                "forecast_periods": matching_periods,
            },
            "reasons": reasons,
            "method": (
                "Rule-based recommendation: umbrella at 40% precipitation or rain/snow "
                "language; jacket at 15°C or below or wind of at least 8 m/s; stronger "
                "outdoor caution for alerts, severe weather, 70% precipitation or "
                "wind of at least 15 m/s."
            ),
            "source": "National Weather Service",
        }
