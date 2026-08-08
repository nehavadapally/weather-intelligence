"""FastMCP server exposing live National Weather Service tools.

Run locally:
    python weather_mcp_server.py

Deploy this folder as its own Databricks App and register the resulting App
URL as a custom external MCP in Databricks Agent Bricks.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from dotenv import load_dotenv
from fastmcp import FastMCP

from weather_adapter import NWSWeatherAdapter, WeatherAdapterError

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

mcp = FastMCP("weather-intelligence")
_adapter: NWSWeatherAdapter | None = None


def get_adapter() -> NWSWeatherAdapter:
    """Create one reusable adapter for connection pooling and caching."""
    global _adapter
    if _adapter is None:
        _adapter = NWSWeatherAdapter()
    return _adapter


def _success(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "success",
        "tool": tool,
        "message": f"{tool} completed successfully.",
        "data": data,
    }


def _error(tool: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "tool": tool,
        "message": message,
        "data": None,
    }


def _run_tool(tool: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Return a consistent response and never expose a raw stack trace."""
    try:
        return _success(tool, operation())
    except WeatherAdapterError as exc:
        logger.warning("%s failed: %s", tool, exc)
        return _error(tool, str(exc))
    except Exception as exc:  # Defensive boundary for remote tool calls.
        logger.exception("Unexpected failure in %s", tool)
        return _error(tool, f"The weather service failed unexpectedly: {exc}")


@mcp.tool
def get_current_weather(location: str) -> dict[str, Any]:
    """Get the latest observed weather for a United States location.

    Use this tool for questions containing words such as "now", "currently",
    or "right now". It resolves a US city/state or latitude/longitude pair and
    returns the nearest NWS station observation.

    Args:
        location: US location as "City, ST" or coordinates as "lat,lon".

    Returns:
        A consistent result containing temperature, humidity, wind,
        conditions, observation time and NWS station information.
    """
    return _run_tool(
        "get_current_weather",
        lambda: get_adapter().get_current_weather(location),
    )


@mcp.tool
def get_weather_forecast(location: str, days: int = 3) -> dict[str, Any]:
    """Get a multi-period weather forecast for a United States location.

    Use this tool for future-looking questions such as tomorrow, this weekend,
    or the next several days. The response includes temperatures,
    precipitation probability, wind, conditions and detailed narrative text.

    Args:
        location: US location as "City, ST" or coordinates as "lat,lon".
        days: Number of forecast days from 1 to 7. Values are safely clamped.

    Returns:
        Forecast metadata and daytime/night-time forecast periods.
    """
    return _run_tool(
        "get_weather_forecast",
        lambda: get_adapter().get_forecast(location, days),
    )


@mcp.tool
def get_weather_recommendation(
    location: str,
    target_date: str | None = None,
) -> dict[str, Any]:
    """Recommend an umbrella, jacket and outdoor-plan precautions.

    This is a derived weather-prediction tool rather than a raw API passthrough.
    It applies documented thresholds to the NWS forecast and active alerts.

    Args:
        location: US location as "City, ST" or coordinates as "lat,lon".
        target_date: Optional "today", "tomorrow", or ISO date YYYY-MM-DD.

    Returns:
        Rule-based recommendations, supporting forecast evidence, active-alert
        count and a clear explanation of the thresholds used.
    """
    return _run_tool(
        "get_weather_recommendation",
        lambda: get_adapter().get_recommendation(location, target_date),
    )


@mcp.tool
def get_active_weather_alerts(location: str) -> dict[str, Any]:
    """Get active National Weather Service alerts for a US location.

    Use this tool for safety-related questions involving warnings, watches,
    severe weather, flooding, storms, tornadoes, snow or hazardous travel.

    Args:
        location: US location as "City, ST" or coordinates as "lat,lon".

    Returns:
        Alert count and normalised event, severity, urgency, timing,
        description and safety-instruction fields.
    """
    return _run_tool(
        "get_active_weather_alerts",
        lambda: get_adapter().get_active_alerts(location),
    )


if __name__ == "__main__":
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", "8000")))
    mcp.run(transport="http", host="0.0.0.0", port=port)
