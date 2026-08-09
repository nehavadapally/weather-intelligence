# Homework 3: Weather-Prediction MCP Server and Agent

## Overview

This submission converts the Weather Intelligence work from Homework 2 into a tool library that a Databricks Agent Bricks agent can call through the Model Context Protocol.

The Homework 2 Flask application, Lakebase weather documents and vector embeddings can remain in the same repository. Homework 3 adds a separate `mcp_server/` Databricks App containing a thin FastMCP layer and a weather adapter responsible for all HTTP, parsing and recommendation logic.

App URL: https://agent-weather-talker-7474645166709307.aws.databricksapps.com/

Evidence at /mcp-evidence

## Architecture

```text
User
  |
  v
Databricks Agent Bricks
  |
  | MCP tool call
  v
mcp_server/weather_mcp_server.py
  |
  | Python adapter call
  v
mcp_server/weather_adapter.py
  |
  | HTTPS
  v
National Weather Service API
```

The MCP server and existing Homework 2 Flask application are deployed independently. This avoids mixing a human-facing REST/UI application with the agent-facing MCP service.

## Weather API choice

The project continues to use the National Weather Service API because:

- It is free.
- It does not require an API key or credit card.
- It provides official United States observations, forecasts and active alerts.
- It allows substantial reuse of the Homework 2 NWS client and location-resolution work.

The limitation is that the implementation only supports locations covered by the National Weather Service. The agent system prompt explicitly prevents the agent from pretending that international coverage is available.

## Repository structure

```text
weather-intelligence/
├── app.py                         # Existing Homework 2 Flask application
├── weather_client.py              # Existing Homework 2 ingestion client
├── weather_sync.py                # Existing Homework 2 synchronisation
├── lakebase.py                    # Existing Homework 2 database helper
├── notebooks/                     # Existing embedding pipeline
├── sql/                           # Existing Lakebase schemas
├── mcp_server/
│   ├── weather_adapter.py         # HTTP, parsing and recommendation logic
│   ├── weather_mcp_server.py      # Thin FastMCP tool definitions
│   ├── app.yaml                   # Databricks App configuration
│   ├── requirements.txt           # Runtime dependencies
│   ├── requirements-dev.txt       # Test dependencies
│   ├── .env.example               # Local configuration example
│   └── tests/
│       └── test_weather_adapter.py
├── agent/
│   ├── SYSTEM_PROMPT.md
│   └── DEMO_QUESTIONS.md
├── README_HOMEWORK_3.md
└── NEXT_STEPS.md
```

## MCP tools

### 1. `get_current_weather(location)`

Retrieves the latest observation from the nearest NWS observation station.

Returns:

- Temperature in Celsius and Fahrenheit
- Conditions
- Relative humidity
- Wind speed and direction
- Observation timestamp
- Observation station

Example agent question:

```text
What is the weather in Chicago, IL right now?
```

### 2. `get_weather_forecast(location, days=3)`

Retrieves daytime and night-time forecast periods for one to seven days.

Returns:

- Period name and timestamps
- Temperature in Celsius and Fahrenheit
- Precipitation probability
- Wind
- Short conditions
- Detailed narrative forecast

Example agent question:

```text
Will it rain in Austin, TX during the next three days?
```

### 3. `get_weather_recommendation(location, target_date=None)`

Applies transparent rule-based logic to forecast and alert data. This satisfies the assignment requirement for a prediction or recommendation that does more than repeat an API response.

Rules:

- Bring an umbrella when precipitation probability is at least 40%, or rain, shower, thunderstorm, snow, sleet or drizzle language is present.
- Bring a jacket when the minimum temperature is 15°C or below, or forecast wind reaches 8 m/s.
- Apply heat caution when the maximum temperature reaches 32°C.
- Recommend avoiding or postponing exposed outdoor activity when an active alert exists, severe-weather language is present, precipitation reaches 70%, or wind reaches 15 m/s.

The response includes the evidence and the method so the agent can explain its recommendation.

Example agent question:

```text
Should I take an umbrella and jacket in New York, NY tomorrow?
```

### 4. `get_active_weather_alerts(location)`

Retrieves active NWS watches, warnings and advisories.

Returns:

- Event and headline
- Severity, urgency and certainty
- Onset and expiry
- Description
- Official instruction text

Example agent question:

```text
Are there any severe-weather warnings for Miami, FL?
```

## Separation of responsibilities

### `weather_adapter.py`

Contains:

- Location resolution
- NWS `/points` calls
- Observation-station lookup
- Current observation retrieval
- Forecast retrieval and normalisation
- Active alert retrieval
- Recommendation thresholds and evidence
- Clean domain errors

### `weather_mcp_server.py`

Contains:

- `FastMCP` initialisation
- `@mcp.tool` definitions
- Tool docstrings
- Consistent success/error response wrappers
- Streamable HTTP server startup

There are no direct HTTP calls inside the MCP tool functions.

## Standard tool response

Successful tools return:

```json
{
  "status": "success",
  "tool": "get_weather_forecast",
  "message": "get_weather_forecast completed successfully.",
  "data": {}
}
```

Failures return:

```json
{
  "status": "error",
  "tool": "get_weather_forecast",
  "message": "The location could not be resolved.",
  "data": null
}
```

This helps the agent handle failures without receiving a raw stack trace.

## Local setup

```bash
cd mcp_server
python -m venv .venv
```

Activate the environment and install dependencies:

```bash
pip install -r requirements-dev.txt
```

Run tests:

```bash
pytest -q
```

Start the MCP server:

```bash
python weather_mcp_server.py
```

The server uses port 8000 unless `PORT` or `DATABRICKS_APP_PORT` is set.

## Databricks deployment

1. Push the Homework 3 files to the `weather-intelligence` repository.
2. Pull the repository into a Databricks Git folder.
3. Create a Databricks MCP Server Starter App, when available.
4. Change the deployment source to the Git folder's `mcp_server/` subfolder.
5. Deploy and check the app logs.
6. Add the deployed app as a Custom MCP Server in Agent Bricks or the AI/ML Playground.
7. Confirm that the four tools are discovered.
8. Copy `agent/SYSTEM_PROMPT.md` into the system-prompt field.
9. Run the questions in `agent/DEMO_QUESTIONS.md`.
10. Capture the tool calls and final answers as submission evidence.

The detailed UI sequence and validation checklist are in `NEXT_STEPS.md`.

## Agent guardrails

The supplied system prompt instructs the agent to:

- Call a tool before stating live weather information.
- Select the correct tool for current conditions, forecasts, recommendations or alerts.
- Never invent weather data.
- State that NWS coverage is United States only.
- Ask for a clearer location when required.
- Treat forecasts as uncertain predictions.
- Follow official instructions during severe weather.
- Explain tool errors rather than guessing.

## Demonstration evidence

Add screenshots to:

```text
evidence/homework-3/
```

Minimum demonstrations:

1. Current conditions in Chicago
2. Three-day forecast for Austin
3. Umbrella and jacket recommendation for New York

Recommended additional test:

4. An international location showing the US-only coverage limitation

## Known limitations

- The National Weather Service is US-only.
- Public Nominatim geocoding can reject shared Databricks cloud addresses. Common cities are resolved from a local catalogue, and coordinates can always be supplied directly.
- Weather recommendations are simple documented rules, not a trained forecasting model.
- Forecast availability depends on NWS service availability.
- The current version does not yet expose Homework 2 Lakebase vector search as an MCP tool.
- A dashboard is not included because it is an optional stretch goal.

## Possible improvements

- Add a `search_weather_context` MCP tool backed by the existing Homework 2 `weather_embeddings` table.
- Add Lakebase tracing for tool name, parameters, status, duration, session and error messages.
- Add Open-Meteo as an international fallback.
- Expand the local US location catalogue.
- Add a dashboard for recent agent questions and tool outcomes.

## Submission checklist

- [ ] FastMCP server deployed as its own Databricks App
- [ ] Separate weather adapter contains all HTTP and parsing logic
- [ ] At least three MCP tools visible in Agent Bricks
- [ ] Recommendation tool applies documented logic
- [ ] Clean error returned for invalid locations and API failures
- [ ] No secret or API key committed
- [ ] Agent system prompt configured
- [ ] Three tool-calling demonstrations captured
- [ ] GitHub repository and App evidence added to this README
