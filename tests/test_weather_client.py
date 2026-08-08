from weather_client import EnvironmentAgencyWeatherClient


def sample_warning():
    return {
        "@id": "https://environment.data.gov.uk/flood-monitoring/id/floods/123",
        "floodAreaID": "123",
        "description": "River Example at Example Town",
        "floodArea": {
            "county": "Somerset",
            "riverOrSea": "River Example"
        },
        "eaAreaName": "Wessex",
        "eaRegionName": "South West",
        "severity": "Flood Alert",
        "severityLevel": 3,
        "message": "Flooding is possible following persistent rainfall.",
        "timeRaised": "2026-08-08T08:00:00Z",
        "timeMessageChanged": "2026-08-08T09:00:00Z",
    }


def test_normalise_warning():
    client = EnvironmentAgencyWeatherClient(base_url="https://example.test")
    document = client._normalise_warning(
        sample_warning(),
        requested_location="Somerset",
        query_latitude=None,
        query_longitude=None,
    )
    assert document is not None
    assert document["id"] == "ea-flood:123"
    assert document["source_type"] == "alert"
    assert document["severity_level"] == 3
    assert document["county"] == "Somerset"
    assert document["river_or_sea"] == "River Example"
    assert "persistent rainfall" in document["narrative_text"]


def test_county_selector_builds_county_filter(monkeypatch):
    client = EnvironmentAgencyWeatherClient(base_url="https://example.test")
    captured = {}

    def fake_get(path, params):
        captured["path"] = path
        captured["params"] = params
        return {"items": [sample_warning()]}

    monkeypatch.setattr(client, "_get", fake_get)
    documents = client.fetch_documents("Somerset", limit=25, min_severity=3)
    assert captured["path"] == "/id/floods"
    assert captured["params"]["county"] == "Somerset"
    assert captured["params"]["min-severity"] == 3
    assert len(documents) == 1


def test_coordinate_selector_builds_geo_filter(monkeypatch):
    client = EnvironmentAgencyWeatherClient(base_url="https://example.test")
    captured = {}

    def fake_get(path, params):
        captured["params"] = params
        return {"items": [sample_warning()]}

    monkeypatch.setattr(client, "_get", fake_get)
    documents = client.fetch_documents("51.5074,-0.1278", radius_km=40)
    assert captured["params"]["lat"] == 51.5074
    assert captured["params"]["long"] == -0.1278
    assert captured["params"]["dist"] == 40
    assert documents[0]["query_latitude"] == 51.5074
