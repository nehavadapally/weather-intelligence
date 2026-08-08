# Required Agent Demonstrations

Capture the Agent Bricks tool call and final answer for at least these three questions.

## 1. Current conditions

**Question:** What is the weather in Chicago, IL right now?

**Expected tool:** `get_current_weather`

Evidence to capture:
- The selected tool and arguments
- The returned temperature, conditions, humidity and wind
- The agent's final answer

## 2. Multi-day forecast

**Question:** Will it rain in Austin, TX during the next three days?

**Expected tool:** `get_weather_forecast`

Evidence to capture:
- `days` set to 3
- Precipitation probability and forecast periods
- The agent's conclusion expressed as a forecast, not a certainty

## 3. Derived recommendation

**Question:** Should I take an umbrella and jacket in New York, NY tomorrow?

**Expected tool:** `get_weather_recommendation`

Evidence to capture:
- The derived recommendation
- Supporting precipitation, temperature, wind and alert evidence
- The documented rule-based reasoning

## Recommended fourth demonstration: limitation handling

**Question:** What is the forecast for London tomorrow?

Expected behaviour:
- The agent explains that this MCP implementation uses the US-only National Weather Service
- It does not invent a London forecast
