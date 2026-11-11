"""WP9: the pipeline end to end with fake sources, a temp database, synthetic
boundary fixtures and a stubbed FEMA lookup. No network, no real profile."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from test_affordability import make_profile
from test_scoring import make_listing

from home_search import cli, config, pipeline
from home_search.digest.render import Email
from home_search.enrich import flood
from home_search.models import ListingKind, ListingStatus, ZoningConfidence
from home_search.sources import browser_inbox, redfin_csv
from home_search.sources.base import SourceUnavailable
from home_search.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
BOUNDARIES = FIXTURES / "boundaries"
CFG = config.load_search()
A = config.load_assumptions()
TODAY = date(2026, 11, 11)                     # a Wednesday
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)

# Points inside the SYNTHETIC boundary rectangles in tests/fixtures/boundaries.
BELLAIRE_CONDIT = (29.705, -95.455)            # Bellaire HS, Pershing MS, Condit El
BELLAIRE_CONDIT_2 = (29.701, -95.456)
BELLAIRE_ONLY = (29.715, -95.49)               # Bellaire HS, no target elementary
BELLAIRE_FLOODWAY = (29.69, -95.48)
LAMAR = (29.70, -95.42)
MEMORIAL = (29.775, -95.51)                    # Memorial HS, Memorial MS, Bunker Hill El


def sale(n, address, zip_code, price, at=None, **over):
    lat, lon = at if at else (None, None)
    return make_listing(n, address=address, zip_code=zip_code, price=price, sqft=3000,
                        lat=lat, lon=lon, **over)


def rental(n, address, zip_code, rent, at=None, **over):
    lat, lon = at if at else (None, None)
    return make_listing(n, address=address, zip_code=zip_code, price=rent, sqft=3000,
                        kind=ListingKind.RENT, lat=lat, lon=lon, **over)


B1 = sale(1, "1 Cheap Ln", "77401", 900_000, BELLAIRE_CONDIT)      # $300/sqft, hot
B2 = sale(2, "2 Middle Ln", "77401", 1_080_000, BELLAIRE_CONDIT_2)
B3 = sale(3, "3 Edge Ln", "77401", 1_200_000, BELLAIRE_ONLY)
FW = sale(4, "4 Bayou Ln", "77401", 1_000_000, BELLAIRE_FLOODWAY)
LM = sale(5, "5 Lamar Ln", "77025", 950_000, LAMAR)
U1 = sale(6, "6 Nocoords Ln", "77401", 1_050_000)                   # zoning unknown
M1 = sale(7, "7 Memorial Dr", "77024", 1_100_000, MEMORIAL)
OUT = sale(8, "8 Downtown St", "77002", 1_000_000)                  # outside every area
KW = sale(9, "9 Auction Ave", "77401", 1_000_000, BELLAIRE_CONDIT,
          description="Sold at auction, as is.")
R1 = rental(10, "10 Rent Ln", "77401", 5_000, BELLAIRE_CONDIT)
R2 = rental(11, "11 Lease Ln", "77024", 6_000)
SALES = [B1, B2, B3, FW, LM, U1, M1, OUT, KW]


class FakeSource:
    def __init__(self, name, listings, warnings=()):
        self.name = name
        self.listings = listings
        self.warnings = list(warnings)

    def fetch(self, cfg):
        return list(self.listings)


class Failing:
    def __init__(self, name, exc):
        self.name = name
        self.exc = exc

    def fetch(self, cfg):
        raise self.exc


@pytest.fixture(autouse=True)
def fake_fema(monkeypatch):
    """FEMA answers from fixtures: floodway at one point, minimal-hazard X elsewhere."""
    calls = []
    payloads = {name: json.loads((FIXTURES / f"nfhl_{name}.json").read_text())
                for name in ("floodway", "x_minimal")}

    def fetch(lat, lon, *, cache_dir=None, transport=None):
        calls.append((lat, lon))
        return payloads["floodway" if (lat, lon) == BELLAIRE_FLOODWAY else "x_minimal"]

    monkeypatch.setattr(flood, "fetch_nfhl", fetch)
    return calls


@pytest.fixture
def mail(monkeypatch):
    """Records hot alerts and digests instead of rendering (WP10) or sending them."""
    sent = {"alerts": [], "weekly": [], "send": []}

    def hot_alert(c):
        sent["alerts"].append((c.listing.key, c.listing.price))
        return Email(subject="hot", html="", text="")

    def weekly(candidates, rentals, changes, market, advisor, outages):
        sent["weekly"].append({"candidates": candidates, "rentals": rentals,
                               "changes": changes, "market": market, "advisor": advisor,
                               "outages": outages})
        return Email(subject="weekly", html="", text="")

    def send(email, dry_run=False):
        sent["send"].append((email.subject, dry_run))
        return "sent"

    monkeypatch.setattr(pipeline.render_mod, "hot_alert", hot_alert)
    monkeypatch.setattr(pipeline.render_mod, "weekly", weekly)
    monkeypatch.setattr(pipeline.send_mod, "send", send)
    return sent


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite") as s:
        yield s


def run(store, sources=None, fn=pipeline.scan, profile=None, **kw):
    if sources is None:
        sources = [FakeSource("fake", SALES + [R1, R2])]
    kw.setdefault("cfg", CFG)
    return fn(sources=sources, store=store, assumptions=A, profile=profile,
              today=kw.pop("today", TODAY), boundary_dir=kw.pop("boundary_dir", BOUNDARIES), **kw)


def keys(cands):
    return {c.listing.key for c in cands}


# ------------------------------------------------------------- end to end


def test_scan_without_profile(store, mail):
    r = run(store, [
        FakeSource("fake", SALES + [R1, R2], warnings=["fake: file x.json record 3 skipped"]),
        Failing("blocked", SourceUnavailable("blocked: blocked (HTTP 403)")),
        Failing("stubbed", NotImplementedError("WP4")),
        Failing("broken", RuntimeError("boom")),
    ])
    assert r.fetched == len(SALES) + 2
    assert keys(r.candidates) == {B1.key, B2.key, B3.key, M1.key}
    assert keys(r.unverified) == {U1.key}
    assert r.dropped == {"floodway": 1, "non_target_high": 1, "excluded_keyword": 1}
    assert r.evaluated == len(SALES) - 1                         # OUT is in no area
    assert keys(r.rentals) == {R1.key, R2.key}
    assert all(c.costs == [] for c in r.rentals)
    assert r.outages[:4] == [
        "fake: file x.json record 3 skipped",
        "blocked: blocked (HTTP 403)",
        "stubbed: not implemented yet (WP4)",
        "broken: failed (RuntimeError)",
    ]
    assert len(r.outages) == 4 and r.failures == {}
    assert {e.kind for e in r.changes} == {"new"}

    by_key = {c.listing.key: c for c in r.candidates + r.unverified}
    for c in by_key.values():
        assert c.affordability is None
        assert c.rent_vs_buy is not None and c.costs
        assert set(c.score_parts) == set(CFG.scoring.weights)
        assert c.score_parts["carrying_cost"] == 50
        assert "new" in c.flags
    assert [c.score for c in r.candidates] == sorted((c.score for c in r.candidates),
                                                     reverse=True)
    b1 = by_key[B1.key]
    assert b1.zoning.confidence == ZoningConfidence.VERIFIED
    assert b1.score_parts["schools"] == 100 and b1.score_parts["value"] > 90
    assert b1.rent_vs_buy.rent_source == "comp:1 rentals"        # R1, same verified zone
    assert by_key[B3.key].score_parts["schools"] == 60
    u1 = by_key[U1.key]
    assert {"zoning_unverified", "flood_unknown"} <= set(u1.flags)
    assert u1.score_parts["schools"] == CFG.scoring.params.schools_unknown
    assert u1.score_parts["flood"] == CFG.scoring.params.flood_unknown
    assert u1.rent_vs_buy.rent_source == "comp:1 rentals"        # same area, zoning unknown


def test_rental_comps_follow_area_and_verified_high(store, mail):
    r = run(store)
    m1 = next(c for c in r.candidates if c.listing.key == M1.key)
    # R2 is in the Memorial area with unknown zoning, so it is a comp for M1.
    assert m1.rent_vs_buy.rent_source == "comp:1 rentals"
    assert m1.rent_vs_buy.rent_equivalent_monthly == R2.price


def test_scan_with_profile(store, mail):
    profile = make_profile()
    r = run(store, profile=profile)
    assert r.candidates
    for c in r.candidates + r.unverified:
        assert c.affordability is not None
        assert c.costs[0].scenario_id == "plan"
        assert c.score_parts["carrying_cost"] != 50 or c.affordability.plan_basis_monthly
    # the 1.2M listing is over the synthetic household's hard ceiling
    assert r.dropped.get("over_hard_ceiling", 0) >= 1
    assert B3.key not in keys(r.candidates)


def test_profile_flood_tolerance_drops_shaded_x(store, mail, monkeypatch):
    payload = json.loads((FIXTURES / "nfhl_x_0_2pct.json").read_text())
    monkeypatch.setattr(flood, "fetch_nfhl", lambda lat, lon, **kw: payload)
    strict = run(store, profile=make_profile())
    assert strict.candidates == [] and strict.dropped["flood_tolerance"] >= 4
    loose = run(store, profile=make_profile(household__flood_tolerance="allow_500yr"))
    assert loose.candidates and all("flood_500yr" in c.flags for c in loose.candidates)


def test_require_verified_high_off_promotes_claims(store, mail):
    raw = CFG.model_dump()
    raw["schools"]["require_verified_high"] = False
    cfg = config.SearchConfig.model_validate(raw)
    claim = sale(20, "20 Claim Ln", "77401", 1_000_000,
                 har_schools={"high": "Bellaire High School"})
    r = run(store, [FakeSource("fake", [B1, U1, claim])], cfg=cfg)
    assert keys(r.candidates) == {B1.key, claim.key}
    assert keys(r.unverified) == {U1.key}            # names no target high


def test_missing_boundaries_reported_and_everything_unverified(store, mail, tmp_path):
    r = run(store, boundary_dir=tmp_path / "none")
    assert r.candidates == []
    assert LM.key in keys(r.unverified)              # not dropped: zoning not verified
    notes = [o for o in r.outages if o.startswith("school boundaries:")]
    assert len(notes) == 2 and "hisd.geojson" in notes[0]


# ------------------------------------------------------------- failures


def test_stage_failure_is_counted_not_fatal(store, mail, monkeypatch):
    real = flood.assess

    def assess(listing):
        if listing.key == B2.key:
            raise ValueError("bad payload")
        return real(listing)

    monkeypatch.setattr(flood, "assess", assess)
    r = run(store)
    assert r.failures == {"flood": 1}
    assert B2.key not in keys(r.candidates) and B1.key in keys(r.candidates)
    assert "pipeline: 1 listing(s) skipped after a failure at the flood stage" in r.outages


def test_mark_missing_only_after_successful_fetch(store, mail):
    run(store, [FakeSource("fake", [B1, B2])])
    for _ in range(3):
        run(store, [Failing("fake", SourceUnavailable("fake: blocked"))])
        run(store, [FakeSource("fake", [])])          # empty inbox: no information
    assert store.get(B2.key).status == ListingStatus.ACTIVE
    run(store, [FakeSource("fake", [B1])])
    r = run(store, [FakeSource("fake", [B1])])
    assert store.get(B2.key).status == ListingStatus.OFF_MARKET
    assert any(e.kind == "removed" and e.key == B2.key for e in r.changes)


def test_all_sources_down_still_scores_stored_listings(store, mail):
    run(store)
    r = run(store, [Failing("fake", SourceUnavailable("fake: blocked"))])
    assert r.fetched == 0 and B1.key in keys(r.candidates)
    assert r.outages == ["fake: blocked"]


# ------------------------------------------------------------- hot alerts


def test_hot_alert_once_per_key_and_price(store, mail):
    r1 = run(store)
    hot = [c for c in r1.candidates if c.score >= CFG.scoring.hot_alert_threshold]
    assert keys(hot) == {B1.key}
    assert r1.alerts_sent == 1 and mail["alerts"] == [(B1.key, 900_000)]
    assert store.was_alerted(B1.key, 900_000)

    r2 = run(store)
    assert r2.alerts_sent == 0 and len(mail["alerts"]) == 1

    cut = B1.model_copy(update={"price": 880_000, "fetched_at": NOW + timedelta(hours=1)})
    r3 = run(store, [FakeSource("fake", [cut, *SALES[1:], R1, R2])])
    b1 = next(c for c in r3.candidates if c.listing.key == B1.key)
    assert "price_cut" in b1.flags
    assert r3.alerts_sent == 1 and mail["alerts"][-1] == (B1.key, 880_000)


def test_unverified_listings_never_alert(store, mail):
    raw = CFG.model_dump()
    raw["scoring"]["hot_alert_threshold"] = 0
    cfg = config.SearchConfig.model_validate(raw)
    r = run(store, cfg=cfg)
    assert r.alerts_sent == len(r.candidates)
    assert U1.key not in {k for k, _ in mail["alerts"]}


def test_dry_run_alert_is_not_recorded(store, mail):
    r = run(store, dry_run=True)
    assert r.alerts_sent == 1 and mail["send"] == [("hot", True)]
    assert not store.was_alerted(B1.key, 900_000)


def test_alert_delivery_not_implemented_is_an_outage(store):
    r = run(store)          # real WP10 stubs raise NotImplementedError
    assert r.alerts_sent == 0
    assert not store.was_alerted(B1.key, 900_000)
    assert any(o.startswith("hot alerts: not sent") for o in r.outages)


# ------------------------------------------------------------- digest


def test_digest_without_profile(store, mail):
    r = run(store, fn=pipeline.digest)
    assert len(mail["weekly"]) == 1
    sent = mail["weekly"][0]
    assert ("weekly", False) in mail["send"]
    snaps = {s.area_id: s for s in sent["market"]}
    assert set(snaps) == {"bellaire", "memorial"}
    bellaire = snaps["bellaire"]
    assert bellaire.week_of == date(2026, 11, 9)                 # Monday of TODAY's week
    # B1, B2, B3, FW, U1, KW (LM is verified to Lamar, OUT is in no area)
    assert bellaire.active_count == 6 and bellaire.median_rent == 5000
    assert bellaire.price_cut_share == 0
    assert store.snapshots("bellaire")[-1] == bellaire
    adv = sent["advisor"]
    assert set(adv) == ADVISOR_KEYS
    assert adv["has_profile"] is False and adv["max_price"] == {}
    assert keys(adv["unverified"]) == {U1.key} and adv["unverified_total"] == 1
    assert adv["portfolio"]["count"] == 5
    assert all(c.score >= CFG.scoring.digest_min_score for c in sent["candidates"])
    assert {e.kind for e in sent["changes"]} == {"new"}
    assert r.changes == sent["changes"]


ADVISOR_KEYS = {
    "has_profile", "portfolio", "price_band", "max_price", "close_date", "cash_at_close",
    "median_candidate_key", "median_candidate_price", "median_candidate_cash_needed",
    "median_candidate_reserve_months", "planning_price", "plan_rate_bracket", "unverified",
    "unverified_total", "candidates_total", "dropped", "stage_failures",
    "missing_boundaries", "assumptions_as_of",
}


def test_digest_with_profile(store, mail):
    profile = make_profile(search__planning_price=1_000_000)
    run(store, fn=pipeline.digest, profile=profile)
    adv = mail["weekly"][0]["advisor"]
    assert adv["has_profile"] is True
    assert set(adv["max_price"]) == {"bellaire", "memorial"}
    assert "plan" in adv["max_price"]["bellaire"]
    assert adv["close_date"] == date(2027, 6, 1)
    assert adv["cash_at_close"] == 300_000 + 5_000 * 6 - 50_000
    assert adv["median_candidate_cash_needed"] > 0
    assert adv["median_candidate_reserve_months"] > 0
    assert set(adv["plan_rate_bracket"]) == {0.06, 0.07}


def test_digest_delivery_not_implemented_is_an_outage(store):
    r = run(store, fn=pipeline.digest)
    assert "weekly digest: not sent, email delivery is not implemented yet" in r.outages
    assert store.snapshots("bellaire")                             # snapshot still saved


def test_digest_uses_store_history_for_price_cut_share(store, mail):
    run(store)
    cut = B2.model_copy(update={"price": 1_000_000, "fetched_at": NOW + timedelta(hours=1)})
    run(store, [FakeSource("fake", [cut])], fn=pipeline.digest)
    bellaire = next(s for s in mail["weekly"][0]["market"] if s.area_id == "bellaire")
    assert bellaire.price_cut_share == pytest.approx(1 / 6, abs=1e-4)


# ------------------------------------------------------------- analyze


def test_analyze_by_key_url_and_address(store, mail):
    run(store)
    kw = {"store": store, "cfg": CFG, "assumptions": A, "profile": None, "today": TODAY,
          "boundary_dir": BOUNDARIES}
    by_key = pipeline.analyze(B1.key, **kw)
    assert by_key.listing.key == B1.key and by_key.score > 0 and by_key.rent_vs_buy
    assert pipeline.analyze(B1.url, **kw).listing.key == B1.key
    assert pipeline.analyze("1 cheap ln", **kw).listing.key == B1.key
    fw = pipeline.analyze(FW.key, **kw)
    assert "disqualified:floodway" in fw.flags and fw.score == 0
    assert pipeline.analyze(R1.key, **kw).listing.kind == ListingKind.RENT
    with pytest.raises(LookupError, match="no active listing"):
        pipeline.analyze("123 Nowhere Rd", **kw)
    with pytest.raises(LookupError, match="listings match"):
        pipeline.analyze(" Ln", **kw)


# ------------------------------------------------------------- CLI


SYNTHETIC_PROFILE = {
    "meta": {"generated": "2026-11-01"},
    "income": {"gross_monthly_for_lender": 43_210},
    "cash_for_purchase": {"balance": 345_678, "monthly_addition": 4_321},
    "carrying_costs": {"total_housing_carry_target_monthly": [7_654, 8_765]},
}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """A laptop with no secrets and no network: every source fails or is empty."""
    for name in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "DIGEST_TO", "ALERT_GMAIL_LABEL",
                 "HOME_SEARCH_ENABLE_HAR_WEB", "HOME_SEARCH_DISABLE_REDFIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROFILE_YAML", yaml.safe_dump(SYNTHETIC_PROFILE))
    monkeypatch.setattr(browser_inbox, "INBOX", tmp_path / "inbox")

    def no_network(client, url):
        raise SourceUnavailable("redfin_csv: request failed (ConnectError)")

    monkeypatch.setattr(redfin_csv, "_get", no_network)
    return tmp_path


def test_cli_scan_dry_run_offline(offline, capsys):
    rc = cli.main(["scan", "--dry-run", "--db", str(offline / "state.sqlite")])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert out.out.startswith("scan complete: 0 listings fetched")
    assert "redfin_csv: request failed (ConnectError)" in out.out
    for value in ("43", "345", "4,321", "4321", "7,654", "7654", "8,765", "8765", "$"):
        assert value not in out.out


def test_cli_sources_flag(offline, capsys):
    rc = cli.main(["scan", "--sources", "har_browser", "--db", str(offline / "s.sqlite")])
    out = capsys.readouterr().out
    assert rc == 0 and "redfin_csv" not in out
    assert cli.main(["scan", "--sources", "nope", "--db", str(offline / "s.sqlite")]) == 1


def test_cli_analyze_unknown_target(offline, capsys):
    assert cli.main(["analyze", "1 Nowhere", "--db", str(offline / "s.sqlite")]) == 1
    assert "no active listing" in capsys.readouterr().err
