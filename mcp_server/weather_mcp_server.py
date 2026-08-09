"""FastMCP server exposing live United States weather tools.

This server delegates all external API work and prediction logic to
``weather_adapter.py``. It exposes concise human-readable MCP content while
also returning complete structured data for the agent.

Run locally:
    python weather_mcp_server.py

Deploy this folder as its own Databricks App and attach the resulting app as a
custom MCP server in Databricks Playground or an Agent App.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult

from weather_adapter import NWSWeatherAdapter, WeatherAdapterError

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

mcp = FastMCP("United States Weather Intelligence")
_adapter: NWSWeatherAdapter | None = None


def get_adapter() -> NWSWeatherAdapter:
    """Create and reuse one adapter for connection pooling and caching."""
    global _adapter

    if _adapter is None:
        _adapter = NWSWeatherAdapter()

    return _adapter


def _success_summary(
    tool: str,
    data: dict[str, Any],
) -> str:
    """Create concise text for the tool-result card."""

    location = str(data.get("location") or "the requested location")

    if tool == "get_current_weather":
        conditions = str(data.get("conditions") or "Conditions unavailable")
        temperature_f = data.get("temperature_f")
        temperature_c = data.get("temperature_c")
        humidity = data.get("humidity_percent")
        observed_at = data.get("observed_at")

        temperatures: list[str] = []

        if temperature_f is not None:
            temperatures.append(f"{temperature_f}°F")

        if temperature_c is not None:
            temperatures.append(f"{temperature_c}°C")

        temperature_text = (
            " / ".join(temperatures)
            if temperatures
            else "temperature unavailable"
        )

        details = [
            f"Current weather retrieved for {location}: "
            f"{conditions}, {temperature_text}."
        ]

        if humidity is not None:
            details.append(f"Humidity: {humidity}%.")

        if observed_at:
            details.append(f"Observed at: {observed_at}.")

        details.append("Source: National Weather Service.")

        return " ".join(details)

    if tool == "get_weather_forecast":
        periods = data.get("periods") or []
        days_requested = data.get("days_requested")
        generated_at = data.get("generated_at")

        summary = (
            f"Weather forecast retrieved for {location}: "
            f"{len(periods)} forecast period(s)"
        )

        if days_requested is not None:
            summary += f" covering up to {days_requested} day(s)"

        summary += "."

        if generated_at:
            summary += f" Generated at: {generated_at}."

        return f"{summary} Source: National Weather Service."

    if tool == "get_weather_recommendation":
        recommendation = data.get("recommendation") or {}
        target_date = data.get("date")
        outdoor_plan = recommendation.get("outdoor_plan")

        summary = (
            f"Weather recommendation generated for {location}"
            f"{f' on {target_date}' if target_date else ''}. "
            f"Umbrella: {'yes' if recommendation.get('bring_umbrella') else 'no'}. "
            f"Jacket: {'yes' if recommendation.get('bring_jacket') else 'no'}. "
            f"Heat caution: "
            f"{'yes' if recommendation.get('heat_caution') else 'no'}."
        )

        if outdoor_plan:
            summary += f" {outdoor_plan}"

        return f"{summary} Source: National Weather Service."

    if tool == "get_active_weather_alerts":
        alert_count = int(data.get("alert_count") or 0)

        return (
            f"Active weather alerts checked for {location}: "
            f"{alert_count} alert(s) found. "
            "Source: National Weather Service."
        )

    return f"{tool} completed successfully."


def _success(
    tool: str,
    data: dict[str, Any],
) -> ToolResult:
    """Return concise display content and complete structured data."""

    payload = {
        "status": "success",
        "tool": tool,
        "message": f"{tool} completed successfully.",
        "data": data,
    }

    return ToolResult(
        content=_success_summary(tool, data),
        structured_content=payload,
    )


def _classify_error(
    message: str,
) -> tuple[str, bool, str, str]:
    """Classify an error and create safe guidance for the agent and user."""

    lowered = message.casefold()

    location_indicators = (
        "location must",
        "could not be resolved",
        "invalid latitude",
        "invalid longitude",
        "invalid coordinates",
        "geocoder returned invalid coordinates",
        "public geocoder rejected",
    )

    if any(indicator in lowered for indicator in location_indicators):
        return (
            "INVALID_LOCATION",
            False,
            (
                "The location could not be recognised. Please provide a "
                "United States city and two-letter state code, such as "
                "'Chicago, IL', or valid US coordinates as 'lat,lon'."
            ),
            (
                "Ask the user to clarify the location. Do not guess which "
                "place they intended."
            ),
        )

    if "date must" in lowered:
        return (
            "INVALID_DATE",
            False,
            (
                "The requested date could not be understood. Please use "
                "'today', 'tomorrow', or YYYY-MM-DD."
            ),
            (
                "Ask the user to provide 'today', 'tomorrow', or a date "
                "formatted as YYYY-MM-DD."
            ),
        )

    if "days must" in lowered:
        return (
            "INVALID_DAYS",
            False,
            (
                "The forecast period is invalid. Please request between "
                "1 and 7 days."
            ),
            "Ask the user to provide a forecast period from 1 to 7 days.",
        )

    if "no forecast period is available" in lowered:
        return (
            "FORECAST_NOT_AVAILABLE",
            False,
            (
                "A forecast is not available for the requested date. "
                "Please choose a date within the available forecast range."
            ),
            (
                "Ask the user to choose a nearer date. Do not estimate "
                "weather for an unavailable date."
            ),
        )

    return (
        "WEATHER_SERVICE_UNAVAILABLE",
        True,
        (
            "The live weather service is temporarily unavailable. "
            "Please refresh the page and try again. If the problem "
            "continues, try again later."
        ),
        (
            "Explain that live weather data could not be retrieved. "
            "Retry once only if appropriate, then stop. Do not estimate, "
            "invent, or substitute weather data from another tool."
        ),
    )


def _error(
    tool: str,
    technical_message: str,
) -> ToolResult:
    """Return a clean error without exposing internal exception details."""

    (
        error_code,
        retryable,
        public_message,
        suggested_action,
    ) = _classify_error(technical_message)

    payload = {
        "status": "error",
        "tool": tool,
        "error_code": error_code,
        "message": public_message,
        "retryable": retryable,
        "suggested_action": suggested_action,
        "data": None,
    }

    return ToolResult(
        content=public_message,
        structured_content=payload,
    )


def _run_tool(
    tool: str,
    operation: Callable[[], dict[str, Any]],
) -> ToolResult:
    """Execute a tool while keeping technical failures out of user output."""

    try:
        return _success(tool, operation())

    except WeatherAdapterError as exc:
        # Controlled adapter failures are logged without a stack trace.
        logger.warning("%s failed: %s", tool, exc)

        return _error(
            tool,
            str(exc),
        )

    except Exception:
        # Unexpected technical details remain only in Databricks App logs.
        logger.exception("Unexpected failure in %s", tool)

        return _error(
            tool,
            "The weather service failed unexpectedly.",
        )


@mcp.tool
def get_current_weather(
    location: str,
) -> ToolResult:
    """Get the latest observed weather for a United States location.

    Use this tool only for present-condition questions containing terms such
    as "now", "currently", "today right now", or "latest observation". It
    resolves a US city and state, or a US latitude/longitude pair, and returns
    the nearest available National Weather Service station observation.

    Args:
        location: A United States location formatted as "City, ST", such as
            "Chicago, IL", or coordinates formatted as "lat,lon".

    Returns:
        A ToolResult with a concise display summary and structured data
        containing location, coordinates, station, observation time,
        conditions, temperature, humidity, wind and source information.

    Error behaviour:
        Invalid or ambiguous locations return a clean INVALID_LOCATION result.
        API or network failures return WEATHER_SERVICE_UNAVAILABLE. Raw stack
        traces and internal exception details are never returned to the user.
    """

    return _run_tool(
        "get_current_weather",
        lambda: get_adapter().get_current_weather(location),
    )


@mcp.tool
def get_weather_forecast(
    location: str,
    days: int = 3,
) -> ToolResult:
    """Get a future weather forecast for a United States location.

    Use this tool for future-looking questions such as tomorrow, this weekend,
    or the next several days. It returns daytime and night-time National
    Weather Service forecast periods with temperatures, precipitation
    probability, wind, conditions and detailed narrative text.

    Args:
        location: A United States location formatted as "City, ST", such as
            "Austin, TX", or coordinates formatted as "lat,lon".
        days: Number of forecast days requested. Values are limited to the
            supported range of 1 to 7 days.

    Returns:
        A ToolResult with a concise display summary and structured forecast
        metadata, forecast periods, requested day count and source.

    Error behaviour:
        Invalid locations or day values return a clean corrective message.
        API or network failures return WEATHER_SERVICE_UNAVAILABLE. Current
        weather must not be presented as a substitute for a failed forecast.
    """

    return _run_tool(
        "get_weather_forecast",
        lambda: get_adapter().get_forecast(location, days),
    )


@mcp.tool
def get_weather_recommendation(
    location: str,
    target_date: str | None = None,
) -> ToolResult:
    """Generate practical US weather recommendations using forecast rules.

    This prediction tool does more than echo raw National Weather Service
    data. It evaluates forecast periods and active alerts using these rules:

    - Recommend an umbrella when precipitation probability is at least 40%,
      or when the forecast mentions rain, showers, thunderstorms, drizzle,
      snow or sleet.
    - Recommend a jacket when the minimum temperature is 15°C or below,
      or when forecast wind reaches at least 8 metres per second.
    - Add heat caution when the maximum temperature reaches at least 32°C.
    - Recommend stronger outdoor caution when there is an active NWS alert,
      severe-weather language, precipitation probability of at least 70%,
      or wind reaching at least 15 metres per second.

    Use this tool when the user asks whether to carry an umbrella, wear a
    jacket, travel, or continue with an outdoor activity.

    Args:
        location: A United States location formatted as "City, ST", such as
            "New York, NY", or coordinates formatted as "lat,lon".
        target_date: Optional date as "today", "tomorrow", or YYYY-MM-DD.
            When omitted, the earliest available forecast date is used.

    Returns:
        A ToolResult with a concise display summary and structured data
        containing umbrella, jacket, heat and outdoor-plan recommendations;
        forecast evidence; active-alert count; reasons; applied thresholds;
        and the National Weather Service source.

    Error behaviour:
        Invalid location or date inputs return corrective guidance. Missing
        forecasts or upstream failures return a clean error. No recommendation
        is invented when supporting weather data is unavailable.
    """

    return _run_tool(
        "get_weather_recommendation",
        lambda: get_adapter().get_recommendation(
            location,
            target_date,
        ),
    )


@mcp.tool
def get_active_weather_alerts(
    location: str,
) -> ToolResult:
    """Get active National Weather Service alerts for a US location.

    Use this tool for safety questions involving warnings, watches, flooding,
    storms, tornadoes, snow, severe weather or hazardous travel.

    Args:
        location: A United States location formatted as "City, ST", such as
            "Miami, FL", or coordinates formatted as "lat,lon".

    Returns:
        A ToolResult with a concise display summary and structured data
        containing alert count, event, headline, severity, urgency, certainty,
        timing, description, safety instructions and source.

    Error behaviour:
        Invalid locations return corrective guidance. API and network failures
        return WEATHER_SERVICE_UNAVAILABLE without exposing a stack trace.
    """

    return _run_tool(
        "get_active_weather_alerts",
        lambda: get_adapter().get_active_alerts(location),
    )


if __name__ == "__main__":
    port = int(
        os.getenv(
            "DATABRICKS_APP_PORT",
            os.getenv("PORT", "8000"),
        )
    )

    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
    )