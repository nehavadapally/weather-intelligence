"""Tests for llm_summary.summarize_search_results - no real network/Databricks
serving-endpoint calls. Covers the fail-soft contract: this function must
never raise, only ever return a string or None.
"""

import llm_summary


def _sample_results() -> list[dict]:
    return [
        {
            "location": "Chicago, IL",
            "headline": "Flash Flood Warning",
            "source_type": "alert",
            "chunk_text": "Flooding is occurring or imminent. Move to higher ground.",
        }
    ]


def test_summarize_returns_none_when_no_results():
    assert llm_summary.summarize_search_results("flooding?", []) is None


def test_summarize_returns_none_when_endpoint_unset(monkeypatch):
    monkeypatch.setattr(llm_summary, "SUMMARY_MODEL_ENDPOINT", "")
    assert llm_summary.summarize_search_results("flooding?", _sample_results()) is None


def test_summarize_returns_none_and_does_not_raise_when_serving_call_fails(monkeypatch):
    class _BoomServingEndpoints:
        @staticmethod
        def query(**kwargs):
            raise RuntimeError("endpoint not found")

    class _BoomClient:
        serving_endpoints = _BoomServingEndpoints()

    monkeypatch.setattr(llm_summary, "_get_workspace_client", lambda: _BoomClient())
    # Should not raise, even though the underlying call does.
    assert llm_summary.summarize_search_results("flooding?", _sample_results()) is None


def test_summarize_returns_model_text_on_success(monkeypatch):
    class _FakeMessage:
        content = "  Chicago is under a flash flood warning right now.  "

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    captured = {}

    class _FakeServingEndpoints:
        @staticmethod
        def query(**kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    class _FakeClient:
        serving_endpoints = _FakeServingEndpoints()

    monkeypatch.setattr(llm_summary, "_get_workspace_client", lambda: _FakeClient())
    summary = llm_summary.summarize_search_results("flooding?", _sample_results())

    assert summary == "Chicago is under a flash flood warning right now."
    assert captured["name"] == llm_summary.SUMMARY_MODEL_ENDPOINT
    assert "flooding?" in captured["messages"][1].content
    assert "Chicago, IL" in captured["messages"][1].content


def test_summarize_truncates_prompt_to_max_results(monkeypatch):
    many_results = [
        {
            "location": f"City {i}, ST",
            "headline": "Forecast",
            "source_type": "forecast",
            "chunk_text": f"chunk {i}",
        }
        for i in range(10)
    ]
    captured = {}

    class _FakeServingEndpoints:
        @staticmethod
        def query(**kwargs):
            captured.update(kwargs)

            class _R:
                class _M:
                    content = "ok"

                choices = [type("C", (), {"message": _M()})]

            return _R()

    class _FakeClient:
        serving_endpoints = _FakeServingEndpoints()

    monkeypatch.setattr(llm_summary, "_get_workspace_client", lambda: _FakeClient())
    llm_summary.summarize_search_results("any query", many_results)

    prompt = captured["messages"][1].content
    included = sum(1 for i in range(10) if f"chunk {i}" in prompt)
    assert included == llm_summary.SUMMARY_MAX_RESULTS_IN_PROMPT