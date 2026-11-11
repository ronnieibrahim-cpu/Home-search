"""WP3: Redfin CSV source. Fixtures are synthetic (fictional street names, fake ids)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from home_search.config import load_search
from home_search.models import ListingKind, ListingStatus
from home_search.sources.base import SourceUnavailable
from home_search.sources.redfin_csv import (
    PAUSE_RANGE_S,
    USER_AGENT,
    RedfinCsv,
    build_url,
    parse_csv,
)

FIX = Path(__file__).parent / "fixtures"
SALE = (FIX / "redfin_sale_77401.csv").read_text()
RENT = (FIX / "redfin_rent_77401.csv").read_text()
EMPTY = (FIX / "redfin_empty.csv").read_text()
BLOCKED = (FIX / "redfin_blocked.html").read_text()


@pytest.fixture
def cfg():
    """Real search.yaml, with areas pinned so tests don't depend on the zip list."""
    base = load_search()
    area = base.areas[0].model_copy(update={"zips": ["77401"], "fringe_zips": ["77096"]})
    return base.model_copy(update={
        "areas": [area],
        "purchase": base.purchase.model_copy(
            update={"price_min": 800_000, "price_max": 1_200_000, "stretch_pct": 0.05}),
        "rent": base.rent.model_copy(
            update={"enabled": True, "monthly_min": 3_500, "monthly_max": 8_000}),
        "home": base.home.model_copy(update={"property_types": ["single_family"]}),
    })


class Recorder:
    """MockTransport handler: serves canned responses, records every request."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def csv_response(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/csv"})


def make_source(handler):
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          headers={"User-Agent": USER_AGENT})
    return RedfinCsv(client=client, sleep=sleeps.append), sleeps


# ------------------------------------------------------------------ parse_csv


def test_parse_sale_fixture():
    listings = parse_csv(SALE, "sale")
    # disclaimer row and PAST SALE row are dropped; everything else parsed
    assert [x.address for x in listings] == [
        "1001 Example Ln", "1003 Sample St", "1005 Placeholder Ct",
        "1007 Fixture Dr", "1009 Testcase Ave", "1011 Mock Blvd",
    ]
    first = listings[0]
    assert first.source == "redfin_csv"
    assert first.source_id == "90000001"
    assert first.url == "https://www.redfin.com/TX/Bellaire/1001-Example-Ln-77401/home/90000001"
    assert first.kind is ListingKind.SALE
    assert first.status is ListingStatus.ACTIVE
    assert (first.city, first.zip) == ("Bellaire", "77401")
    assert first.lat == pytest.approx(29.7051)
    assert first.lon == pytest.approx(-95.4612)
    assert first.price == 975_000
    assert (first.beds, first.baths) == (4.0, 3.5)
    assert (first.sqft, first.lot_sqft, first.year_built) == (3120, 7200, 2008)
    assert first.days_on_market == 12
    assert first.hoa_monthly is None
    assert first.fetched_at.tzinfo is not None

    by_addr = {x.address: x for x in listings}
    assert by_addr["1003 Sample St"].status is ListingStatus.PENDING
    assert by_addr["1003 Sample St"].hoa_monthly == 25
    assert by_addr["1007 Fixture Dr"].price == 1_450_000          # "1,450,000"
    assert by_addr["1011 Mock Blvd"].lot_sqft is None             # blank cell
    assert by_addr["1011 Mock Blvd"].status is ListingStatus.ACTIVE  # Coming Soon


def test_parse_rent_fixture():
    listings = parse_csv(RENT, "rent")
    assert len(listings) == 5
    assert all(x.kind is ListingKind.RENT for x in listings)
    assert all(x.status is ListingStatus.ACTIVE for x in listings)
    first = listings[0]
    assert (first.address, first.price, first.source_id) == ("2001 Example Ln", 5_800, "90000101")
    assert (first.lat, first.lon) == (pytest.approx(29.7061), pytest.approx(-95.4622))
    assert listings[1].price == 7_500


def test_parse_header_only_is_zero_rows_not_a_block():
    assert parse_csv(EMPTY, "sale") == []


def test_parse_handles_bom():
    assert len(parse_csv("﻿" + SALE, "sale")) == 6


@pytest.mark.parametrize("text", [BLOCKED, "", "   \n", "foo,bar\n1,2\n"])
def test_parse_non_csv_raises(text):
    with pytest.raises(SourceUnavailable):
        parse_csv(text, "sale")


# --------------------------------------------------------------------- fetch


def test_fetch_filters_band_type_and_zip(cfg):
    def respond(req):
        return csv_response(RENT if req.url.params.get("isRentals") else SALE)

    rec = Recorder(respond)
    src, _ = make_source(rec)
    got = src.fetch(cfg)

    sale = sorted(x.address for x in got if x.kind is ListingKind.SALE)
    rent = sorted(x.address for x in got if x.kind is ListingKind.RENT)
    # 1005 townhouse dropped (type); 1007 over stretch_max (1.26M); 1009 under
    # price_min; 1011 at 1.25M is inside stretch (1.26M) so kept.
    assert sale == ["1001 Example Ln", "1003 Sample St", "1011 Mock Blvd"]
    # 2005 townhouse dropped; 2007 under monthly_min; 2009 over monthly_max.
    assert rent == ["2001 Example Ln", "2003 Sample St"]
    # Two zips searched x two kinds; served fixture repeats per zip but dedupes.
    assert len(rec.requests) == 4
    assert len(got) == 5


def test_fetch_one_request_per_zip_per_kind_with_jittered_pause(cfg):
    rec = Recorder(lambda req: csv_response(EMPTY))
    src, sleeps = make_source(rec)
    assert src.fetch(cfg) == []

    seen = [(r.url.params["region_id"], bool(r.url.params.get("isRentals")))
            for r in rec.requests]
    assert sorted(seen) == sorted({("77401", False), ("77096", False),
                                   ("77401", True), ("77096", True)})
    assert len(seen) == len(set(seen))
    assert len(sleeps) == len(rec.requests) - 1
    lo, hi = PAUSE_RANGE_S
    assert all(lo <= s <= hi for s in sleeps)
    for r in rec.requests:
        assert r.headers["user-agent"] == USER_AGENT
        assert r.url.host == "www.redfin.com"
        assert "/home/" not in r.url.path                       # no detail pages


def test_fetch_skips_rentals_when_disabled(cfg):
    cfg = cfg.model_copy(update={"rent": cfg.rent.model_copy(update={"enabled": False})})
    rec = Recorder(lambda req: csv_response(EMPTY))
    src, _ = make_source(rec)
    src.fetch(cfg)
    assert len(rec.requests) == 2
    assert not any(r.url.params.get("isRentals") for r in rec.requests)


def test_fetch_reads_zips_from_config(cfg):
    area = cfg.areas[0].model_copy(update={"zips": ["77024", "77079"], "fringe_zips": []})
    cfg = cfg.model_copy(update={"areas": [area]})
    rec = Recorder(lambda req: csv_response(EMPTY))
    src, _ = make_source(rec)
    src.fetch(cfg)
    assert {r.url.params["region_id"] for r in rec.requests} == {"77024", "77079"}


@pytest.mark.parametrize("status", [403, 429])
def test_fetch_block_status_raises_without_retry(cfg, status):
    rec = Recorder(lambda req: httpx.Response(status, text="nope"))
    src, sleeps = make_source(rec)
    with pytest.raises(SourceUnavailable, match=str(status)):
        src.fetch(cfg)
    assert len(rec.requests) == 1                              # no retry, run stops
    assert sleeps == []


def test_fetch_html_instead_of_csv_raises(cfg):
    rec = Recorder(lambda req: httpx.Response(
        200, text=BLOCKED, headers={"content-type": "text/html; charset=utf-8"}))
    src, _ = make_source(rec)
    with pytest.raises(SourceUnavailable, match="HTML"):
        src.fetch(cfg)
    assert len(rec.requests) == 1


def test_fetch_html_body_with_csv_content_type_raises(cfg):
    rec = Recorder(lambda req: csv_response(BLOCKED))
    src, _ = make_source(rec)
    with pytest.raises(SourceUnavailable):
        src.fetch(cfg)


def test_fetch_block_midway_stops_and_returns_nothing_partial(cfg):
    def respond(req):
        if len(rec.requests) == 1:
            return csv_response(SALE)
        return httpx.Response(429)

    rec = Recorder(respond)
    src, _ = make_source(rec)
    with pytest.raises(SourceUnavailable):
        src.fetch(cfg)
    assert len(rec.requests) == 2


def test_fetch_other_http_error_raises(cfg):
    rec = Recorder(lambda req: httpx.Response(503))
    src, _ = make_source(rec)
    with pytest.raises(SourceUnavailable):
        src.fetch(cfg)
    assert len(rec.requests) == 1


def test_fetch_transport_error_raises(cfg):
    def respond(req):
        raise httpx.ConnectError("refused", request=req)

    rec = Recorder(respond)
    src, _ = make_source(rec)
    with pytest.raises(SourceUnavailable):
        src.fetch(cfg)
    assert len(rec.requests) == 1


def test_fetch_zero_rows_is_not_a_block(cfg):
    rec = Recorder(lambda req: csv_response(EMPTY))
    src, _ = make_source(rec)
    assert src.fetch(cfg) == []                                # no exception


def test_build_url():
    sale = httpx.URL(build_url("77401", "sale", ["single_family", "townhouse"]))
    assert sale.params["region_id"] == "77401"
    assert sale.params["region_type"] == "2"
    assert sale.params["uipt"] == "1,3"
    assert "isRentals" not in sale.params
    rent = httpx.URL(build_url("77401", ListingKind.RENT, ["single_family"]))
    assert rent.params["isRentals"] == "true"


# ---------------------------------------------------------------------- live


@pytest.mark.network
def test_live_fetch_one_zip():
    """By hand only (pytest -m network). Makes 2 requests: 77401 sale + rent."""
    base = load_search()
    area = base.areas[0].model_copy(update={"zips": ["77401"], "fringe_zips": []})
    cfg = base.model_copy(update={"areas": [area]})
    listings = RedfinCsv().fetch(cfg)
    for x in listings:
        assert x.zip == "77401"
        assert x.source == "redfin_csv"
