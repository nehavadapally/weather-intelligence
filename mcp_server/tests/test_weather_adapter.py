"""Unit tests for the NWS weather adapter.

These tests do not call the internet. They use fake responses or monkeypatch
adapter methods so they are safe to run in CI and before Databricks deployment.
"""

from __future__ import annotations

from typing import Any

import pytest

from weather_adapter import NWSWeatherAdapter, WeatherAdapterError


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, params=None, timeout=None):
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected URL: {url}")
        return FakeResponse(self.responses[url])


def test_known_city_resolves_without_network() -> None:
    session = FakeSession({})
    adapter = NWSWeatherAdapter(session=session)

    resolved = adapter.resolve_location("Chicago, IL")

    assert resolved.latitude == pytest.approx(41.8781)
    assert resolved.longitude == pytest.approx(-87.6298)
    assert session.calls == []


def test_coordinate_validation() -> None:
    adapter = NWSWeatherAdapter(session=FakeSession({}))

    resolved = adapter.resolve_location("40.7128,-74.0060")
    assert resolved.latitude == pytest.approx(40.7128)

    with pytest.raises(WeatherAdapterError, match="Invalid latitude"):
        adapter.resolve_location("100,-74")


def test_current_weather_parsing() -> None:
    point_url = "https://api.weather.gov/points/41.8781,-87.6298"
    stations_url = "https://api.weather.gov/gridpoints/LOT/75,73/stations"
    station_url = "https://api.weather.gov/stations/KORD"
    latest_url = f"{station_url}/observations/latest"

    session = FakeSession(
        {
            point_url: {"properties": {"observationStations": stations_url}},
            stations_url: {
                "features": [
                    {"id": station_url, "properties": {"name": "Chicago O'Hare"}}
                ]
            },
            latest_url: {
                "properties": {
                    "timestamp": "2026-08-08T12:00:00+00:00",
                    "textDescription": "Partly Cloudy",
                    "temperature": {"value": 20.0},
                    "relativeHumidity": {"value": 60.0},
                    "windSpeed": {"value": 5.0},
                    "windDirection": {"value": 270.0},
                }
            },
        }
    )
    adapter = NWSWeatherAdapter(session=session)

    result = adapter.get_current_weather("Chicago, IL")

    assert result["conditions"] == "Partly Cloudy"
    assert result["temperature_c"] == 20.0
    assert result["temperature_f"] == 68.0
    assert result["humidity_percent"] == 60.0
    assert result["station"] == "Chicago O'Hare"


def test_recommendation_applies_thresholds(monkeypatch) -> None:
    adapter = NWSWeatherAdapter(session=FakeSession({}))

    monkeypatch.setattr(
        adapter,
        "get_forecast",
        lambda location, days=7: {
            "location": "Austin, TX",
            "time_zone": "America/Chicago",
            "periods": [
                {
                    "name": "Sunday",
                    "start_time": "2026-08-09T06:00:00-05:00",
                    "temperature_c": 14.0,
                    "precipitation_probability_percent": 60.0,
                    "wind_speed": "10 mph",
                    "conditions": "Rain Showers",
                    "detailed_forecast": "Rain showers are likely.",
                },
                {
                    "name": "Sunday Night",
                    "start_time": "2026-08-09T18:00:00-05:00",
                    "temperature_c": 10.0,
                    "precipitation_probability_percent": 50.0,
                    "wind_speed": "8 mph",
                    "conditions": "Showers",
                    "detailed_forecast": "Showers continue overnight.",
                },
            ],
        },
    )
    monkeypatch.setattr(
        adapter,
        "get_active_alerts",
        lambda location: {"alerts": [], "alert_count": 0},
    )

    result = adapter.get_recommendation("Austin, TX", "2026-08-09")

    assert result["recommendation"]["bring_umbrella"] is True
    assert result["recommendation"]["bring_jacket"] is True
    assert result["evidence"]["maximum_precipitation_probability_percent"] == 60.0
    assert "40% precipitation" in result["method"]
    assert "15°C or below" in result["method"]
    assert "8 m/s" in result["method"]
    assert "70% precipitation" in result["method"]
    assert "15 m/s" in result["method"]
    assert result["source"] == "National Weather Service"
