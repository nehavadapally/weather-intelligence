import pytest
import requests

from weather_client import (
    NWSWeatherClient,
    ResolvedLocation,
    WeatherClientError,
    clean_text,
    stable_hash,
)


def make_client() -> NWSWeatherClient:
    return NWSWeatherClient(nws_base_url="https://example.test", geocoder_base_url="https://geocode.test")


def _location(lat=41.8781, lon=-87.6298, label="Chicago, IL") -> ResolvedLocation:
    return ResolvedLocation(requested=label, label=label, latitude=lat, longitude=lon)


def _sample_alert() -> dict:
    return {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abc",
        "properties": {
            "id": "urn:oid:2.49.0.1.840.0.abc",
            "event": "Flash Flood Warning",
            "headline": "Flash Flood Warning issued for Cook County",
            "description": "A Flash Flood Warning means flooding is occurring or imminent.",
            "instruction": "Move to higher ground immediately.",
            "sent": "2026-08-08T08:00:00+00:00",
            "effective": "2026-08-08T08:05:00+00:00",
        },
    }


def _sample_period() -> dict:
    return {
        "number": 1,
        "name": "Tonight",
        "startTime": "2026-08-08T18:00:00-05:00",
        "detailedForecast": "Showers likely, mainly after 9pm. Low around 68.",
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_clean_text_collapses_whitespace():
    assert clean_text("  a\n\nb   c ") == "a b c"


def test_clean_text_handles_none():
    assert clean_text(None) == ""


def test_stable_hash_is_deterministic_and_input_sensitive():
    assert stable_hash("a", "b") == stable_hash("a", "b")
    assert stable_hash("a", "b") != stable_hash("a", "c")


# ---------------------------------------------------------------------------
# resolve_location
# ---------------------------------------------------------------------------


def test_resolve_location_parses_coordinates_without_geocoding(monkeypatch):
    client = make_client()

    def fail_get(*args, **kwargs):
        raise AssertionError("coordinates shouldn't hit the geocoder")

    monkeypatch.setattr(client, "_get", fail_get)
    resolved = client.resolve_location("41.8781,-87.6298")
    assert resolved.latitude == pytest.approx(41.8781)
    assert resolved.longitude == pytest.approx(-87.6298)
    assert resolved.label == "41.8781,-87.6298"


def test_resolve_location_rejects_out_of_range_coordinates():
    client = make_client()
    with pytest.raises(WeatherClientError):
        client.resolve_location("200,-87.6298")


def test_resolve_location_uses_builtin_table_without_any_network_call(monkeypatch):
    # New York, NY is in _KNOWN_US_PLACES - resolving it must never touch
    # the network, so the geocoder can be 403-blocked (as it was in
    # production - see the "Nominatim 403" incident) and this still works.
    client = make_client()

    def fail_get(*args, **kwargs):
        raise AssertionError("a built-in place shouldn't hit the network at all")

    monkeypatch.setattr(client, "_get", fail_get)
    resolved = client.resolve_location("New York, NY")
    assert resolved.latitude == pytest.approx(40.7128)
    assert resolved.longitude == pytest.approx(-74.0060)


def test_resolve_location_builtin_table_lookup_is_case_insensitive(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        client, "_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network"))
    )
    resolved = client.resolve_location("new york, ny")
    assert resolved.latitude == pytest.approx(40.7128)


def test_get_raises_actionable_error_on_geocoder_403(monkeypatch):
    # Reproduces the real production failure: Nominatim's public server
    # blocks shared cloud/PaaS egress IPs with a 403, independent of
    # User-Agent. The raw requests.HTTPError shouldn't leak out as-is -
    # _get() should explain what's actually going on.
    client = make_client()

    class _FakeResponse:
        status_code = 403

        def raise_for_status(self):
            error = requests.HTTPError("403 Client Error: Forbidden")
            error.response = self
            raise error

    monkeypatch.setattr(
        client.session, "get", lambda url, params=None, timeout=None: _FakeResponse()
    )

    with pytest.raises(WeatherClientError) as excinfo:
        client._get(f"{client.geocoder_base_url}/search", params={"q": "Nowhereville"})

    message = str(excinfo.value)
    assert "403" in message
    assert "Nominatim" in message
    assert "_KNOWN_US_PLACES" in message


def test_get_403_from_nws_is_not_treated_as_geocoder_block(monkeypatch):
    # The special-cased 403 message is specific to the geocoder host - an
    # NWS 403 (unlikely, but possible) should fall through to the generic
    # error path instead of claiming to be a Nominatim block it isn't.
    client = make_client()

    class _FakeResponse:
        status_code = 403

        def raise_for_status(self):
            error = requests.HTTPError("403 Client Error: Forbidden")
            error.response = self
            raise error

    monkeypatch.setattr(
        client.session, "get", lambda url, params=None, timeout=None: _FakeResponse()
    )

    with pytest.raises(WeatherClientError) as excinfo:
        client._get(f"{client.nws_base_url}/points/41.8781,-87.6298")

    assert "Nominatim" not in str(excinfo.value)


def test_resolve_location_rejects_empty_string():
    client = make_client()
    with pytest.raises(WeatherClientError):
        client.resolve_location("   ")


def test_resolve_location_geocodes_city_state(monkeypatch):
    # Ann Arbor, MI is deliberately NOT in _KNOWN_US_PLACES, so this
    # exercises the real Nominatim fallback path.
    client = make_client()
    captured = {}

    def fake_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return [
            {
                "lat": "42.2808",
                "lon": "-83.7430",
                "display_name": "Ann Arbor, Washtenaw County, Michigan, United States",
            }
        ]

    monkeypatch.setattr(client, "_get", fake_get)
    resolved = client.resolve_location("Ann Arbor, MI")
    assert resolved.latitude == pytest.approx(42.2808)
    assert resolved.longitude == pytest.approx(-83.7430)
    assert resolved.label == "Ann Arbor, Washtenaw County, Michigan, United States"
    assert captured["params"]["q"] == "Ann Arbor, MI"
    assert captured["params"]["countrycodes"] == "us"


def test_resolve_location_caches_geocode_results(monkeypatch):
    client = make_client()
    call_count = {"n": 0}

    def fake_get(url, params=None):
        call_count["n"] += 1
        return [{"lat": "42.2808", "lon": "-83.7430", "display_name": "Ann Arbor, MI"}]

    monkeypatch.setattr(client, "_get", fake_get)
    client.resolve_location("Ann Arbor, MI")
    client.resolve_location("ann arbor, mi")  # same key once case-folded
    assert call_count["n"] == 1


def test_resolve_location_raises_when_geocoder_finds_nothing(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "_get", lambda url, params=None: [])
    with pytest.raises(WeatherClientError):
        client.resolve_location("Nowhereville, ZZ")


def test_resolve_location_raises_on_malformed_geocoder_response(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "_get", lambda url, params=None: [{"lat": "not-a-number", "lon": "-87"}])
    with pytest.raises(WeatherClientError):
        client.resolve_location("Somewhere Weird")


# ---------------------------------------------------------------------------
# _normalise_alert / _normalise_forecast
# ---------------------------------------------------------------------------


def test_normalise_alert_joins_description_and_instruction():
    client = make_client()
    document = client._normalise_alert(_sample_alert(), _location())
    # feature["id"] (the full alert URL) wins over properties["id"] (the bare
    # urn) since `feature.get("id") or properties.get("id")` short-circuits
    # on the first truthy value.
    assert document["id"] == "nws-alert:https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abc"
    assert document["source_type"] == "alert"
    assert document["headline"] == "Flash Flood Warning issued for Cook County"
    assert "flooding is occurring" in document["narrative_text"]
    assert "higher ground" in document["narrative_text"]
    assert document["issued_at"] == "2026-08-08T08:00:00+00:00"
    assert document["location"] == "Chicago, IL"
    assert document["latitude"] == pytest.approx(41.8781)


def test_normalise_alert_falls_back_to_hash_id_when_nws_omits_one():
    client = make_client()
    alert = _sample_alert()
    del alert["id"]
    del alert["properties"]["id"]
    document = client._normalise_alert(alert, _location())
    assert document["id"].startswith("nws-alert:")
    assert document["id"] != "nws-alert:"


def test_normalise_alert_returns_none_without_narrative_text():
    client = make_client()
    alert = {"properties": {"event": "Test", "description": "", "instruction": ""}}
    assert client._normalise_alert(alert, _location()) is None


def test_normalise_forecast_builds_stable_id_from_location_and_period():
    client = make_client()
    forecast = {"properties": {"generatedAt": "2026-08-08T12:00:00+00:00"}}
    doc1 = client._normalise_forecast(_sample_period(), forecast, _location())
    doc2 = client._normalise_forecast(_sample_period(), forecast, _location())
    assert doc1["id"] == doc2["id"]
    assert doc1["id"].startswith("nws-forecast:")
    assert doc1["source_type"] == "forecast"
    assert doc1["headline"] == "Tonight"
    assert "Showers likely" in doc1["narrative_text"]
    assert doc1["issued_at"] == "2026-08-08T12:00:00+00:00"


def test_normalise_forecast_returns_none_without_detailed_forecast():
    client = make_client()
    period = {"number": 1, "name": "Tonight", "detailedForecast": ""}
    assert client._normalise_forecast(period, {"properties": {}}, _location()) is None


# ---------------------------------------------------------------------------
# fetch_documents (end-to-end within the client, network mocked)
# ---------------------------------------------------------------------------


def test_fetch_documents_combines_alerts_and_forecast(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "resolve_location", lambda location: _location(label=location))
    monkeypatch.setattr(
        client, "get_point_metadata", lambda lat, lon: {"properties": {"forecast": "https://example.test/forecast"}}
    )
    monkeypatch.setattr(client, "get_active_alerts", lambda lat, lon: [_sample_alert()])
    monkeypatch.setattr(
        client,
        "get_forecast",
        lambda point_metadata: {
            "properties": {"generatedAt": "2026-08-08T12:00:00+00:00", "periods": [_sample_period()]}
        },
    )

    documents = client.fetch_documents("Chicago, IL", limit=50)
    assert {doc["source_type"] for doc in documents} == {"alert", "forecast"}
    assert len(documents) == 2


def test_fetch_documents_respects_limit(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "resolve_location", lambda location: _location(label=location))
    monkeypatch.setattr(client, "get_point_metadata", lambda lat, lon: {"properties": {"forecast": "https://x"}})
    monkeypatch.setattr(client, "get_active_alerts", lambda lat, lon: [_sample_alert(), _sample_alert()])
    monkeypatch.setattr(client, "get_forecast", lambda point_metadata: {"properties": {"periods": [_sample_period()]}})

    documents = client.fetch_documents("Chicago, IL", limit=1)
    assert len(documents) == 1


def test_fetch_documents_raises_weather_client_error_for_bad_location(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "_get", lambda url, params=None: [])  # geocoder finds nothing
    with pytest.raises(WeatherClientError):
        client.fetch_documents("Nowhereville, ZZ")