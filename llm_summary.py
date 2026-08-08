"""Optional LLM-generated natural-language summary of weather search results.

Stretch goal: "Add a GET /weather/search?query=... variant that also returns
an LLM-generated natural-language summary of the top results (basic RAG)."

Uses Databricks Model Serving's Foundation Model APIs via the same
WorkspaceClient the rest of the app already uses for Lakebase secrets - no
extra API key or secret to configure, just a serving endpoint your workspace
has enabled (Foundation Model APIs ship a handful of pay-per-token chat
endpoints out of the box; check Compute -> Serving in your workspace for the
exact name, and make sure the app's service principal has "Can Query" on
it). Point SUMMARY_MODEL_ENDPOINT at whichever one is available to you -
older workspaces/regions may need the "system.ai.<name>" form instead of
"databricks-<name>".

This is intentionally optional and fails soft: if the endpoint name is
unset, the workspace doesn't have Foundation Model APIs enabled, or the
query call fails for any reason, callers get None back (and the failure is
logged, not raised) so /weather/search still returns the vector-search
results even when summarization can't run.
"""

from __future__ import annotations

import logging
import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

logger = logging.getLogger("nws-weather-intelligence")

SUMMARY_MODEL_ENDPOINT = os.environ.get("SUMMARY_MODEL_ENDPOINT", "databricks-claude-sonnet-4-5")
SUMMARY_MAX_TOKENS = int(os.environ.get("SUMMARY_MAX_TOKENS", "400"))
# How many of the top search results to actually feed to the model - keeps
# the prompt bounded even if a caller passes a large top_k.
SUMMARY_MAX_RESULTS_IN_PROMPT = int(os.environ.get("SUMMARY_MAX_RESULTS_IN_PROMPT", "5"))

_SYSTEM_PROMPT = (
    "You are a concise weather-briefing assistant. You only use the "
    "passages you're given - you never invent conditions, locations, or "
    "numbers that aren't in them."
)

_workspace_client: WorkspaceClient | None = None


def _get_workspace_client() -> WorkspaceClient:
    global _workspace_client
    if _workspace_client is None:
        _workspace_client = WorkspaceClient()
    return _workspace_client


def _build_prompt(query: str, results: list[dict]) -> str:
    lines = [f'User question: "{query}"', "", "Retrieved weather passages (most relevant first):"]
    for i, row in enumerate(results[:SUMMARY_MAX_RESULTS_IN_PROMPT], start=1):
        location = row.get("location") or "Unknown location"
        headline = row.get("headline") or ""
        source_type = row.get("source_type") or ""
        chunk_text = row.get("chunk_text") or ""
        lines.append(f"{i}. [{source_type}] {headline} ({location}): {chunk_text}")
    lines.append("")
    lines.append(
        "Write a 2-4 sentence natural-language answer to the user's question "
        "using ONLY the passages above. Mention the specific location(s) and "
        "whether the risk comes from an active alert or a forecast. If the "
        "passages don't actually answer the question, say so plainly instead "
        "of guessing."
    )
    return "\n".join(lines)


def summarize_search_results(query: str, results: list[dict]) -> str | None:
    """Return an LLM-written natural-language summary of `results`, or None.

    Returns None (never raises) whenever a summary can't be produced - no
    results to summarize, no endpoint configured, or the serving endpoint
    call failing - so callers can always fall back to showing raw results.
    """
    if not results:
        return None
    if not SUMMARY_MODEL_ENDPOINT:
        return None

    prompt = _build_prompt(query, results)
    try:
        response = _get_workspace_client().serving_endpoints.query(
            name=SUMMARY_MODEL_ENDPOINT,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ChatMessage(role=ChatMessageRole.USER, content=prompt),
            ],
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        # Missing/disabled Foundation Model APIs, wrong endpoint name, quota,
        # network - none of these should take down search results.
        logger.exception(
            "LLM summary failed (endpoint=%s) - returning results without a summary",
            SUMMARY_MODEL_ENDPOINT,
        )
        return None