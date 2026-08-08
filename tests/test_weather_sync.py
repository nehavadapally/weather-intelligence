"""Tests for weather_sync.run_weather_sync - the shared harvest+upsert path
used by both POST /weather/sync and the scheduled job
(jobs/scheduled_weather_sync.py). No real Lakebase connection or NWS network
calls: NWSWeatherClient and the DB write are both replaced with fakes.
"""

import pytest

import weather_sync


class _FakeClient:
    def __init__(self, docs_by_location: dict):
        self._docs_by_location = docs_by_location

    def fetch_documents(self, location, limit=50):
        if location not in self._docs_by_location:
            raise weather_sync.WeatherClientError(f"unknown location {location!r}")
        return self._docs_by_location[location][:limit]


def _sample_doc(doc_id="nws-alert:1", location="Chicago, IL") -> dict:
    return {
        "id": doc_id,
        "location": location,
        "latitude": 41.8781,
        "longitude": -87.6298,
        "source_type": "alert",
        "headline": "Flash Flood Warning",
        "narrative_text": "Flooding is occurring.",
        "issued_at": "2026-08-08T08:00:00+00:00",
        "effective_at": "2026-08-08T08:00:00+00:00",
        "payload": {"raw": True},
    }


def test_run_weather_sync_rejects_non_list_locations():
    # `locations if locations else DEFAULT_LOCATIONS` only falls back on a
    # *falsy* value ([], None, ""), so the only way to actually hit the
    # `isinstance(locations, list)` guard is a truthy non-list value, e.g. a
    # bare string instead of a list of strings.
    with pytest.raises(ValueError):
        weather_sync.run_weather_sync("Chicago, IL", 50)


def test_run_weather_sync_treats_empty_list_as_use_defaults(monkeypatch):
    # Documenting existing (harmless) behavior: an empty list is falsy, so it
    # falls back to DEFAULT_LOCATIONS rather than raising. Nobody calls
    # /weather/sync meaning "sync literally zero locations," so this is
    # treated as intentional rather than "fixed" into a 400.
    monkeypatch.setattr(weather_sync, "DEFAULT_LOCATIONS", ["Chicago, IL"])
    monkeypatch.setattr(
        weather_sync, "NWSWeatherClient", lambda: _FakeClient({"Chicago, IL": [_sample_doc()]})
    )
    monkeypatch.setattr(weather_sync, "_upsert_weather_documents", lambda documents: len(documents))

    result = weather_sync.run_weather_sync([], 50)
    assert result["locations"] == ["Chicago, IL"]


def test_run_weather_sync_dedupes_by_id_and_upserts(monkeypatch):
    docs = [_sample_doc(), _sample_doc()]  # identical id twice -> should dedupe to 1

    monkeypatch.setattr(weather_sync, "NWSWeatherClient", lambda: _FakeClient({"Chicago, IL": docs}))

    upserted = {}

    def fake_upsert(documents):
        upserted["documents"] = documents
        return len(documents)

    monkeypatch.setattr(weather_sync, "_upsert_weather_documents", fake_upsert)

    result = weather_sync.run_weather_sync(["Chicago, IL"], 50)
    assert result["unique_documents"] == 1
    assert result["synced"] == 1
    assert result["errors"] == []
    assert len(upserted["documents"]) == 1


def test_run_weather_sync_collects_per_location_errors_without_failing(monkeypatch):
    monkeypatch.setattr(
        weather_sync, "NWSWeatherClient", lambda: _FakeClient({"Chicago, IL": [_sample_doc()]})
    )
    monkeypatch.setattr(weather_sync, "_upsert_weather_documents", lambda documents: len(documents))

    result = weather_sync.run_weather_sync(["Chicago, IL", "Nowhereville"], 50)
    assert result["synced"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["location"] == "Nowhereville"


def test_run_weather_sync_skips_non_string_locations(monkeypatch):
    monkeypatch.setattr(weather_sync, "NWSWeatherClient", lambda: _FakeClient({}))
    monkeypatch.setattr(weather_sync, "_upsert_weather_documents", lambda documents: len(documents))

    result = weather_sync.run_weather_sync([123, "  "], 50)
    assert result["synced"] == 0
    assert len(result["errors"]) == 2


def test_run_weather_sync_falls_back_to_default_locations(monkeypatch):
    monkeypatch.setattr(weather_sync, "DEFAULT_LOCATIONS", ["Chicago, IL"])
    monkeypatch.setattr(
        weather_sync, "NWSWeatherClient", lambda: _FakeClient({"Chicago, IL": [_sample_doc()]})
    )
    monkeypatch.setattr(weather_sync, "_upsert_weather_documents", lambda documents: len(documents))

    result = weather_sync.run_weather_sync(None, 50)
    assert result["locations"] == ["Chicago, IL"]
    assert result["synced"] == 1