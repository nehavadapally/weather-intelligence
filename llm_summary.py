"""Optional LLM summary of weather vector-search results.

The feature uses a Databricks Model Serving chat endpoint.

It deliberately fails softly. A serving error must not prevent the user from
receiving the underlying pgvector search results.
"""

from __future__ import annotations

import logging
import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import (
    ChatMessage,
    ChatMessageRole,
)

logger = logging.getLogger(
    "nws-weather-intelligence"
)

SUMMARY_MODEL_ENDPOINT = os.environ.get(
    "SUMMARY_MODEL_ENDPOINT",
    "",
).strip()

SUMMARY_MAX_TOKENS = int(
    os.environ.get(
        "SUMMARY_MAX_TOKENS",
        "400",
    )
)

SUMMARY_MAX_RESULTS_IN_PROMPT = int(
    os.environ.get(
        "SUMMARY_MAX_RESULTS_IN_PROMPT",
        "5",
    )
)

_SYSTEM_PROMPT = (
    "You are a concise weather-briefing assistant. "
    "Use only the retrieved passages supplied to you. "
    "Do not invent conditions, locations, warnings or numbers. "
    "Clearly distinguish active alerts from forecasts."
)

_workspace_client: WorkspaceClient | None = None


def summary_is_configured() -> bool:
    """Return whether a serving endpoint has been configured."""
    return bool(SUMMARY_MODEL_ENDPOINT)


def _get_workspace_client() -> WorkspaceClient:
    """Create and reuse the Databricks workspace client."""
    global _workspace_client

    if _workspace_client is None:
        _workspace_client = WorkspaceClient()

    return _workspace_client


def _build_prompt(
    query: str,
    results: list[dict],
) -> str:
    """Create a grounded RAG prompt from retrieved chunks."""
    lines = [
        f'User question: "{query}"',
        "",
        (
            "Retrieved weather passages, ordered from most "
            "to least relevant:"
        ),
    ]

    for index, row in enumerate(
        results[:SUMMARY_MAX_RESULTS_IN_PROMPT],
        start=1,
    ):
        location = (
            row.get("location")
            or "Unknown location"
        )

        headline = (
            row.get("headline")
            or "Weather update"
        )

        source_type = (
            row.get("source_type")
            or "weather"
        )

        chunk_text = (
            row.get("chunk_text")
            or ""
        )

        lines.append(
            (
                f"{index}. [{source_type}] "
                f"{headline} ({location}): "
                f"{chunk_text}"
            )
        )

    lines.extend(
        [
            "",
            (
                "Write a two to four sentence answer using only "
                "the retrieved passages. Mention the relevant "
                "locations. State whether each important point "
                "comes from an alert or a forecast. If the passages "
                "do not answer the question, say so clearly."
            ),
        ]
    )

    return "\n".join(lines)


def summarize_search_results(
    query: str,
    results: list[dict],
) -> str | None:
    """Return an LLM summary or None when unavailable.

    This function never raises an endpoint error to the caller.
    Vector search must remain usable without the optional summary.
    """
    if not results:
        return None

    if not summary_is_configured():
        return None

    prompt = _build_prompt(
        query,
        results,
    )

    try:
        response = (
            _get_workspace_client()
            .serving_endpoints
            .query(
                name=SUMMARY_MODEL_ENDPOINT,
                messages=[
                    ChatMessage(
                        role=ChatMessageRole.SYSTEM,
                        content=_SYSTEM_PROMPT,
                    ),
                    ChatMessage(
                        role=ChatMessageRole.USER,
                        content=prompt,
                    ),
                ],
                max_tokens=SUMMARY_MAX_TOKENS,
            )
        )

        if not response.choices:
            logger.error(
                "Summary endpoint returned no choices: %s",
                SUMMARY_MODEL_ENDPOINT,
            )
            return None

        content = (
            response
            .choices[0]
            .message
            .content
        )

        if not content:
            logger.error(
                "Summary endpoint returned empty content: %s",
                SUMMARY_MODEL_ENDPOINT,
            )
            return None

        return content.strip()

    except Exception:
        logger.exception(
            (
                "LLM summary failed for endpoint %s. "
                "Returning vector-search results without a summary."
            ),
            SUMMARY_MODEL_ENDPOINT,
        )
        return None