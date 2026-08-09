"""Tests for clean, agent-friendly MCP tool responses."""

from __future__ import annotations

from weather_adapter import WeatherAdapterError
from weather_mcp_server import _run_tool


def raise_error(message: str):
    """Create an operation that raises a controlled adapter error."""

    def operation():
        raise WeatherAdapterError(message)

    return operation


def test_bad_location_returns_clean_error() -> None:
    result = _run_tool(
        "get_current_weather",
        raise_error(
            "The location could not be resolved. "
            "Use 'City, ST' or latitude and longitude as 'lat,lon'."
        ),
    )

    assert result["status"] == "error"
    assert result["tool"] == "get_current_weather"
    assert result["error_code"] == "INVALID_LOCATION"
    assert result["retryable"] is False
    assert "Ask the user" in result["suggested_action"]
    assert result["data"] is None
    assert "Traceback" not in str(result)


def test_api_outage_returns_retryable_error() -> None:
    result = _run_tool(
        "get_weather_forecast",
        raise_error(
            "Weather request failed: connection timed out."
        ),
    )

    assert result["status"] == "error"
    assert result["tool"] == "get_weather_forecast"
    assert (
        result["error_code"]
        == "WEATHER_SERVICE_UNAVAILABLE"
    )
    assert result["retryable"] is True
    assert "Do not invent weather data" in result["suggested_action"]
    assert result["data"] is None
    assert "Traceback" not in str(result)


def test_invalid_date_returns_correction_guidance() -> None:
    result = _run_tool(
        "get_weather_recommendation",
        raise_error(
            "date must be 'today', 'tomorrow', or YYYY-MM-DD."
        ),
    )

    assert result["status"] == "error"
    assert result["error_code"] == "INVALID_DATE"
    assert result["retryable"] is False
    assert "YYYY-MM-DD" in result["suggested_action"]