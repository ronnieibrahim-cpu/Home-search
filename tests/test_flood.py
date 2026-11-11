"""Flood risk (WP6).

The NFHL fixtures in tests/fixtures/nfhl_*.json follow the documented ArcGIS
REST query response shape of NFHL layer 28; see each file's `_fixture_note`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from home_search.enrich import flood
from home_search.models import Listing, ListingKind

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def make_listing(lat: float | None = 29.70001, lon: float | None = -95.46001, description=None):
    return Listing(
        source="test",
        source_id="1",
        url="https://example.invalid/1",
        kind=ListingKind.SALE,
        address="100 Example St",
        city="Bellaire",
        zip="77401",
        lat=lat,
        lon=lon,
        price=1_000_000,
        description=description,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- mapping from recorded responses -------------------------------------------


@pytest.mark.parametrize(
    "fixture, zone, in_floodway",
    [
        ("nfhl_x_minimal", "X", False),
        ("nfhl_x_0_2pct", "X_shaded", False),
        ("nfhl_x_levee", "X_shaded", False),
        ("nfhl_x_blank_subtype", "X_shaded", False),
        ("nfhl_ae", "AE", False),
        ("nfhl_ao", "AE", False),
        ("nfhl_floodway", "floodway", True),
        ("nfhl_edge_x_ae", "AE", False),
        ("nfhl_edge_x_unknown", None, False),
        ("nfhl_zone_d", None, False),
        ("nfhl_empty", None, False),
        ("nfhl_error", None, False),
    ],
)
def test_classify_fixture(fixture, zone, in_floodway):
    risk = flood.classify_response(load(fixture))
    assert risk.fema_zone == zone
    assert risk.in_floodway is in_floodway
    if zone is None:
        assert risk.notes, "an unknown zone must carry a note explaining why"


@pytest.mark.parametrize(
    "attrs, zone",
    [
        ({"FLD_ZONE": "A", "SFHA_TF": "T"}, "AE"),
        ({"FLD_ZONE": "AH", "SFHA_TF": "T"}, "AE"),
        ({"FLD_ZONE": "VE", "SFHA_TF": "T"}, "AE"),
        ({"FLD_ZONE": "V", "SFHA_TF": "T"}, "AE"),
        ({"FLD_ZONE": "A99", "SFHA_TF": "T"}, "AE"),
        ({"FLD_ZONE": "A12"}, "AE"),
        ({"FLD_ZONE": "ae ", "SFHA_TF": "t"}, "AE"),
        ({"FLD_ZONE": "AE", "ZONE_SUBTY": "ADMINISTRATIVE FLOODWAY"}, "floodway"),
        ({"FLD_ZONE": "AE", "ZONE_SUBTY": "COMMUNITY ENCROACHMENT AREA"}, "floodway"),
        ({"FLD_ZONE": "X", "ZONE_SUBTY": "0.2 PCT ANNUAL CHANCE FLOOD HAZARD CONTAINED IN CHANNEL"},
         "X_shaded"),
        ({"FLD_ZONE": "X", "ZONE_SUBTY": "1 PCT FUTURE CONDITIONS"}, "X_shaded"),
        ({"FLD_ZONE": "X", "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD", "SFHA_TF": "T"}, "AE"),
        ({"FLD_ZONE": "B"}, "X_shaded"),
        ({"FLD_ZONE": "C"}, "X"),
        ({"FLD_ZONE": "OPEN WATER"}, None),
        ({"FLD_ZONE": "AREA NOT INCLUDED"}, None),
        ({"FLD_ZONE": "Q"}, None),
        ({}, None),
    ],
)
def test_classify_feature(attrs, zone):
    assert flood.classify_feature(attrs)[0] == zone


def test_unrecognized_zone_is_never_x():
    risk = flood.classify_response(
        {"features": [{"attributes": {"FLD_ZONE": "ZZ", "ZONE_SUBTY": "NEW THING"}}]}
    )
    assert risk.fema_zone is None
    assert any("ZZ / NEW THING" in n for n in risk.notes)


def test_only_minimal_hazard_subtype_maps_to_x():
    # Any X subtype other than minimal hazard must fall to X_shaded.
    for sub in ["0.2 PCT ANNUAL CHANCE FLOOD HAZARD", "AREA WITH REDUCED FLOOD RISK DUE TO LEVEE",
                "SOMETHING NEW", "", None]:
        zone, _ = flood.classify_feature({"FLD_ZONE": "X", "ZONE_SUBTY": sub, "SFHA_TF": "F"})
        assert zone == "X_shaded", sub


# --- description keywords -------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Never flooded! Lovely updated kitchen.", [flood.NOTE_CLAIMS_NO_FLOODING]),
        ("Home did not flood in Harvey.", [flood.NOTE_CLAIMS_NO_FLOODING]),
        ("Has not flooded per seller.", [flood.NOTE_CLAIMS_NO_FLOODING]),
        ("Flooded in 2017, fully remediated.",
         [flood.NOTE_PRIOR_FLOODING, flood.NOTE_REMEDIATED]),
        ("Took on water during Harvey.", [flood.NOTE_PRIOR_FLOODING]),
        ("Home was elevated in 2019.", [flood.NOTE_ELEVATED]),
        ("Never flooded, though the street flooded in 2017.",
         [flood.NOTE_PRIOR_FLOODING, flood.NOTE_CLAIMS_NO_FLOODING]),
        ("Mold remediation completed.", [flood.NOTE_REMEDIATED]),
        ("Elevated ceilings and a pool.", [flood.NOTE_ELEVATED]),
        ("Flood-light wired patio. Close to Floodman Park.", []),
        ("", []),
        (None, []),
    ],
)
def test_description_notes(text, expected):
    assert flood.description_notes(text) == expected


def test_never_flooded_note_does_not_read_as_prior_flooding():
    notes = flood.description_notes("NEVER FLOODED")
    assert flood.NOTE_PRIOR_FLOODING not in notes


# --- assess: cache, timeout, failure ---------------------------------------------


def _transport(payload=None, status=200, calls=None, exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if exc is not None:
            raise exc
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Point the cache at tmp_path and route HTTP through a swappable mock transport."""
    state = {"transport": None}
    real_fetch = flood.fetch_nfhl

    def fake_fetch(lat, lon, *, cache_dir=None, transport=None):
        return real_fetch(lat, lon, cache_dir=tmp_path, transport=state["transport"])

    monkeypatch.setattr(flood, "fetch_nfhl", fake_fetch)
    return state


def test_assess_uses_and_writes_cache(patched, tmp_path):
    calls: list[httpx.Request] = []
    patched["transport"] = _transport(load("nfhl_x_minimal"), calls=calls)
    first = flood.assess(make_listing())
    second = flood.assess(make_listing(lat=29.700012, lon=-95.460008))   # same 5-dp key
    assert first.fema_zone == second.fema_zone == "X"
    assert len(calls) == 1
    assert len(list(tmp_path.glob("nfhl_*.json"))) == 1
    params = calls[0].url.params
    assert params["geometry"] == "-95.46001,29.70001"
    assert params["inSR"] == "4326" and params["f"] == "json"


def test_assess_does_not_cache_errors(patched, tmp_path):
    calls: list[httpx.Request] = []
    patched["transport"] = _transport(load("nfhl_error"), calls=calls)
    risk = flood.assess(make_listing())
    assert risk.fema_zone is None
    assert any(n.startswith(flood.NOTE_LOOKUP_FAILED) for n in risk.notes)
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize(
    "transport_kwargs, reason",
    [
        ({"exc": httpx.ConnectTimeout("timed out")}, "ConnectTimeout"),
        ({"exc": httpx.ReadTimeout("timed out")}, "ReadTimeout"),
        ({"payload": {}, "status": 503}, "HTTP 503"),
        ({"payload": {}, "status": 403}, "HTTP 403"),
    ],
)
def test_assess_failure_returns_none_with_note(patched, transport_kwargs, reason):
    patched["transport"] = _transport(**transport_kwargs)
    risk = flood.assess(make_listing(description="Never flooded."))
    assert risk.fema_zone is None
    assert risk.in_floodway is False
    assert f"{flood.NOTE_LOOKUP_FAILED}: {reason}." in risk.notes
    assert flood.NOTE_CLAIMS_NO_FLOODING in risk.notes


def test_assess_non_object_json(patched):
    patched["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2]))
    risk = flood.assess(make_listing())
    assert risk.fema_zone is None
    assert f"{flood.NOTE_LOOKUP_FAILED}: TypeError." in risk.notes


def test_assess_non_json_body(patched):
    patched["transport"] = httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>maintenance</html>")
    )
    risk = flood.assess(make_listing())
    assert risk.fema_zone is None
    assert any(n.startswith(flood.NOTE_LOOKUP_FAILED) for n in risk.notes)


def test_assess_without_coords_skips_lookup(patched):
    calls: list[httpx.Request] = []
    patched["transport"] = _transport(load("nfhl_ae"), calls=calls)
    risk = flood.assess(make_listing(lat=None, lon=None, description="Flooded in 2017."))
    assert calls == []
    assert risk.fema_zone is None
    assert risk.notes == [flood.NOTE_NO_COORDS, flood.NOTE_PRIOR_FLOODING]


def test_assess_combines_zone_and_description(patched):
    patched["transport"] = _transport(load("nfhl_floodway"))
    risk = flood.assess(make_listing(description="Elevated after Harvey."))
    assert risk.fema_zone == "floodway"
    assert risk.in_floodway is True
    assert risk.notes == [flood.NOTE_ELEVATED]


def test_client_timeout_is_ten_seconds():
    assert flood.TIMEOUT_S == 10.0


# --- live ------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize(
    "lat, lon",
    [
        (29.7058, -95.4589),   # Bellaire city centre, 77401
        (29.7650, -95.5150),   # Memorial, 77024
    ],
)
def test_live_nfhl(lat, lon, tmp_path):
    payload = flood.fetch_nfhl(lat, lon, cache_dir=tmp_path)
    assert "features" in payload, payload
    risk = flood.classify_response(payload)
    assert risk.fema_zone in {"X", "X_shaded", "AE", "floodway"}, risk
