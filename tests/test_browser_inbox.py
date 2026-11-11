"""WP3c: browser inbox source, and `enabled_sources`. Fixtures are synthetic
(fictional street and school names, fake MLS numbers)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from home_search.config import load_search
from home_search.models import Listing, ListingKind, ListingStatus
from home_search.sources import browser_inbox
from home_search.sources.base import enabled_sources
from home_search.sources.browser_inbox import BrowserInbox, MalformedInboxFile, parse_file

FIX = Path(__file__).parent / "fixtures"
HAS_HAR_SCHOOLS = "har_schools" in Listing.model_fields
MTIME = datetime(2026, 11, 10, 7, 30, tzinfo=UTC)


@pytest.fixture
def cfg():
    """Real search.yaml with zips and bands pinned so tests don't depend on it."""
    base = load_search()
    a0 = base.areas[0].model_copy(update={"zips": ["77401"], "fringe_zips": ["77096"]})
    a1 = base.areas[1].model_copy(update={"zips": ["77024", "77055"], "fringe_zips": []})
    return base.model_copy(update={
        "areas": [a0, a1],
        "purchase": base.purchase.model_copy(
            update={"price_min": 800_000, "price_max": 1_200_000, "stretch_pct": 0.05}),
        "rent": base.rent.model_copy(
            update={"enabled": True, "monthly_min": 3_500, "monthly_max": 8_000}),
    })


def put(inbox: Path, fixture: str, name: str | None = None) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / (name or fixture)
    shutil.copy(FIX / fixture, dest)
    ts = MTIME.timestamp()
    os.utime(dest, (ts, ts))
    return dest


def write(inbox: Path, name: str, data) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / name
    dest.write_text(json.dumps(data))
    return dest


def rec(**over):
    base = {"source_id": "90000901", "url": "https://www.har.com/homedetail/90000901",
            "kind": "sale", "status": "active", "address": "9001 Example Ln",
            "city": "Bellaire", "zip": "77401", "price": 900_000}
    base.update(over)
    return base


# ------------------------------------------------------------------ valid file


def test_valid_file_ingests_and_moves_to_done(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    path = put(inbox, "inbox_valid.json", "har-20261110-0730.json")
    src = BrowserInbox(inbox=inbox)

    got = {x.source_id: x for x in src.fetch(cfg)}

    # 90000104 is above stretch_max, 90000105 is outside the search zips.
    assert set(got) == {"90000101", "90000102", "90000103"}
    assert src.warnings == []
    assert not path.exists()
    assert (inbox / "done" / "har-20261110-0730.json").exists()

    a = got["90000101"]
    assert a.source == "har_browser"
    assert a.url.startswith("https://www.har.com/homedetail/")
    assert a.kind is ListingKind.SALE
    assert a.status is ListingStatus.ACTIVE
    assert (a.address, a.city, a.zip) == ("1001 Example Ln", "Bellaire", "77401")
    assert (a.price, a.beds, a.baths, a.sqft) == (950_000, 4, 3.5, 3200)
    assert (a.lot_sqft, a.year_built, a.days_on_market) == (8500, 2012, 6)
    assert a.listed_on == date(2026, 11, 5)
    assert a.fetched_at == MTIME

    b = got["90000102"]
    assert b.status is ListingStatus.PENDING          # "Option Pending"
    assert b.price == 1_150_000                       # "$1,150,000"
    assert b.lot_sqft is None and b.year_built is None and b.listed_on is None

    r = got["90000103"]
    assert r.kind is ListingKind.RENT and r.price == 5200


def test_har_schools_passed_through_when_model_has_field(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    put(inbox, "inbox_valid.json")
    got = {x.source_id: x for x in BrowserInbox(inbox=inbox).fetch(cfg)}
    if HAS_HAR_SCHOOLS:
        assert got["90000101"].har_schools == {
            "elementary": "Sample Elementary", "middle": "Sample Middle", "high": "Sample High"}
        assert got["90000102"].har_schools == {"high": "Sample High"}   # nulls/blanks dropped
        assert got["90000103"].har_schools is None
    else:
        assert all("har_schools" not in x.model_dump() for x in got.values())


def test_har_schools_dropped_when_model_lacks_field(tmp_path, monkeypatch):
    """Simulates the model without the field: records still validate."""
    monkeypatch.setattr(browser_inbox, "_model_has_har_schools", lambda: False)
    path = write(tmp_path, "x.json", [rec(har_schools={"high": "Sample High"})])
    out = parse_file(path)
    assert len(out) == 1 and out[0].address == "9001 Example Ln"
    assert getattr(out[0], "har_schools", None) is None


def test_rentals_dropped_when_rent_disabled(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    put(inbox, "inbox_valid.json")
    cfg = cfg.model_copy(update={"rent": cfg.rent.model_copy(update={"enabled": False})})
    got = BrowserInbox(inbox=inbox).fetch(cfg)
    assert {x.kind for x in got} == {ListingKind.SALE}


def test_rent_band_applies(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    write(inbox, "a.json", [rec(source_id="1", kind="rent", price=3_000),
                            rec(source_id="2", kind="rent", price=4_000),
                            rec(source_id="3", kind="rent", price=9_000)])
    got = BrowserInbox(inbox=inbox).fetch(cfg)
    assert [x.source_id for x in got] == ["2"]


def test_later_file_wins_for_same_listing(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    write(inbox, "har-20261109-0700.json", [rec(price=950_000)])
    write(inbox, "har-20261110-0700.json", [rec(price=925_000)])
    got = BrowserInbox(inbox=inbox).fetch(cfg)
    assert len(got) == 1 and got[0].price == 925_000
    assert sorted(p.name for p in (inbox / "done").iterdir()) == [
        "har-20261109-0700.json", "har-20261110-0700.json"]


def test_name_clash_in_done_does_not_overwrite(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    (inbox / "done").mkdir(parents=True)
    (inbox / "done" / "a.json").write_text("[]")
    write(inbox, "a.json", [rec()])
    BrowserInbox(inbox=inbox).fetch(cfg)
    assert sorted(p.name for p in (inbox / "done").iterdir()) == ["a.1.json", "a.json"]
    assert (inbox / "done" / "a.json").read_text() == "[]"


# ------------------------------------------------------------- bad input


def test_bad_records_skipped_with_warnings(tmp_path, cfg, caplog):
    inbox = tmp_path / "inbox"
    path = put(inbox, "inbox_mixed.json")
    src = BrowserInbox(inbox=inbox)
    with caplog.at_level(logging.WARNING, logger="home_search.sources.browser_inbox"):
        got = src.fetch(cfg)

    assert [x.source_id for x in got] == ["90000201"]
    w = src.warnings
    assert len(w) == 7
    assert "record 1 skipped, not an object" in w[0]
    assert "record 2 skipped, URL is not on har.com" in w[1]   # har.com.example.net
    assert "record 3 skipped, URL is not on har.com" in w[2]
    assert "record 4 skipped, missing price" in w[3]
    assert "record 5 skipped, missing address" in w[4]
    assert "record 6 skipped, invalid kind" in w[5]
    assert "record 7 skipped, invalid listed_on" in w[6]
    assert all(m.startswith("har_browser: inbox_mixed.json ") for m in w)
    # warnings never echo file text
    assert not any("Ignore previous" in m for m in w)
    assert [r.getMessage() for r in caplog.records] == w
    # a file that was read moves to done/ even with bad records in it
    assert not path.exists() and (inbox / "done" / "inbox_mixed.json").exists()


def test_malformed_json_skips_file_and_leaves_it(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    bad = put(inbox, "inbox_malformed.json")
    good = put(inbox, "inbox_valid.json")
    src = BrowserInbox(inbox=inbox)

    got = src.fetch(cfg)

    assert len(got) == 3                       # the valid file still ingests
    assert bad.exists()                        # left in place
    assert not (inbox / "done" / "inbox_malformed.json").exists()
    assert not good.exists()
    assert len(src.warnings) == 1
    assert "inbox_malformed.json skipped, not valid JSON" in src.warnings[0]


def test_top_level_not_array_is_malformed(tmp_path, cfg):
    path = write(tmp_path / "inbox", "obj.json", {"listings": [rec()]})
    src = BrowserInbox(inbox=tmp_path / "inbox")
    assert src.fetch(cfg) == []
    assert path.exists()
    assert "top level is not a JSON array" in src.warnings[0]
    with pytest.raises(MalformedInboxFile):
        parse_file(path)


@pytest.mark.parametrize("url", [
    "https://har.com/homedetail/1",
    "https://www.har.com/homedetail/1",
    "https://WWW.HAR.COM/homedetail/1",
])
def test_har_urls_accepted(tmp_path, url):
    assert len(parse_file(write(tmp_path, "x.json", [rec(url=url)]))) == 1


@pytest.mark.parametrize("url", [
    None, "", "www.har.com/homedetail/1", "javascript:alert(1)",
    "https://evilhar.com/homedetail/1", "https://har.com.evil.example/1",
    "https://har.com@evil.example/1", "ftp://www.har.com/1",
    "https://www.redfin.com/TX/Bellaire/home/1",
])
def test_non_har_urls_rejected(tmp_path, url):
    assert parse_file(write(tmp_path, "x.json", [rec(url=url)])) == []


@pytest.mark.parametrize("over", [
    {"price": True}, {"price": "call for price"}, {"price": float("nan")},
    {"address": 12}, {"kind": None}, {"status": "auction"}, {"status": 3},
])
def test_odd_values_rejected(tmp_path, over):
    assert parse_file(write(tmp_path, "x.json", [rec(**over)])) == []


def test_missing_source_id_falls_back_to_url(tmp_path):
    out = parse_file(write(tmp_path, "x.json", [rec(source_id=None)]))
    assert out[0].source_id == "https://www.har.com/homedetail/90000901"


def test_warnings_reset_each_fetch(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    write(inbox, "a.json", ["junk"])
    src = BrowserInbox(inbox=inbox)
    src.fetch(cfg)
    assert len(src.warnings) == 1
    src.fetch(cfg)
    assert src.warnings == []


# ------------------------------------------------------------- empty inbox


def test_absent_inbox_returns_empty(tmp_path, cfg):
    src = BrowserInbox(inbox=tmp_path / "nope")
    assert src.fetch(cfg) == []
    assert src.warnings == []
    assert not (tmp_path / "nope").exists()


def test_empty_inbox_returns_empty(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    (inbox / "done").mkdir(parents=True)
    (inbox / "done" / "old.json").write_text(json.dumps([rec()]))   # already ingested
    (inbox / "notes.txt").write_text("not json")
    assert BrowserInbox(inbox=inbox).fetch(cfg) == []
    assert (inbox / "notes.txt").exists()


def test_empty_array_file_moves_to_done(tmp_path, cfg):
    inbox = tmp_path / "inbox"
    write(inbox, "a.json", [])
    assert BrowserInbox(inbox=inbox).fetch(cfg) == []
    assert (inbox / "done" / "a.json").exists()


def test_default_inbox_is_data_inbox():
    assert BrowserInbox().inbox == browser_inbox.INBOX
    assert browser_inbox.INBOX.parts[-2:] == ("data", "inbox")


# ------------------------------------------------------------ enabled_sources

_ENV = ("GMAIL_USER", "GMAIL_APP_PASSWORD", "HOME_SEARCH_ENABLE_HAR_WEB",
        "HOME_SEARCH_DISABLE_REDFIN")


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def names(sources):
    return [s.name for s in sources]


def test_enabled_sources_default(clean_env):
    assert names(enabled_sources()) == ["har_browser", "redfin_csv"]


def test_enabled_sources_all_on(clean_env):
    clean_env.setenv("GMAIL_USER", "someone@example.com")
    clean_env.setenv("GMAIL_APP_PASSWORD", "dummy")
    clean_env.setenv("HOME_SEARCH_ENABLE_HAR_WEB", "1")
    assert names(enabled_sources()) == ["har_browser", "email_alerts", "har_web", "redfin_csv"]


@pytest.mark.parametrize("env", [
    {"GMAIL_USER": "someone@example.com"},
    {"GMAIL_APP_PASSWORD": "dummy"},
    {"GMAIL_USER": "", "GMAIL_APP_PASSWORD": "dummy"},
])
def test_email_needs_both_secrets(clean_env, env):
    for k, v in env.items():
        clean_env.setenv(k, v)
    assert "email_alerts" not in names(enabled_sources())


def test_har_web_needs_exactly_1(clean_env):
    clean_env.setenv("HOME_SEARCH_ENABLE_HAR_WEB", "true")
    assert "har_web" not in names(enabled_sources())


def test_redfin_can_be_disabled(clean_env):
    clean_env.setenv("HOME_SEARCH_DISABLE_REDFIN", "1")
    assert names(enabled_sources()) == ["har_browser"]


def test_enabled_sources_only(clean_env):
    clean_env.setenv("HOME_SEARCH_ENABLE_HAR_WEB", "1")
    assert names(enabled_sources(only=["redfin_csv", "har_web"])) == ["har_web", "redfin_csv"]
    # a named source whose prerequisites are missing is still left out
    assert names(enabled_sources(only=["email_alerts"])) == []
    assert enabled_sources(only=[]) == []


def test_enabled_sources_only_unknown_name(clean_env):
    with pytest.raises(ValueError, match="unknown source"):
        enabled_sources(only=["zillow"])


def test_numeric_zip_and_mls_number_accepted(tmp_path):
    out = parse_file(write(tmp_path, "x.json", [rec(zip=77401, source_id=90000901)]))
    assert (out[0].zip, out[0].source_id) == ("77401", "90000901")
