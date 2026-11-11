"""WP9: scoring parts, disqualifiers and flags (docs/ARCHITECTURE.md "Scoring").

Synthetic listings and profiles only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from test_affordability import make_profile

from home_search import config, scoring
from home_search.enrich.flood import NOTE_CLAIMS_NO_FLOODING, NOTE_PRIOR_FLOODING
from home_search.models import (
    Affordability,
    Candidate,
    FloodRisk,
    Listing,
    ListingKind,
    LoanCost,
    MarketSnapshot,
    SchoolZoning,
    ZoningConfidence,
)

CFG = config.load_search()
P = CFG.scoring.params
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)

BELLAIRE_FULL = SchoolZoning(
    district="HISD", high="Bellaire High School", middle="Pershing Middle School",
    elementary="Condit Elementary", confidence=ZoningConfidence.VERIFIED,
)


def make_listing(n: int = 1, *, zip_code: str = "77401", price: int = 1_000_000,
                 kind: ListingKind = ListingKind.SALE, address: str | None = None,
                 **over) -> Listing:
    fields = {
        "source": "fake", "source_id": f"id{n}",
        "url": f"https://example.test/{kind.value}/{n}", "kind": kind,
        "address": address or f"{100 + n} Test St", "city": "Bellaire", "zip": zip_code,
        "price": price, "beds": 4, "baths": 3, "sqft": 3000, "lot_sqft": 7000,
        "year_built": 2005, "days_on_market": 10, "fetched_at": NOW,
    }
    fields.update(over)
    return Listing(**fields)


def make_cost(total: float = 7000.0, *, scenario_id: str = "conventional_20",
              jumbo: bool = False) -> LoanCost:
    return LoanCost(
        scenario_id=scenario_id, down_payment=200_000, loan_amount=800_000, rate=0.065,
        is_jumbo=jumbo, principal_interest=total, property_tax=0, homeowners_insurance=0,
        flood_insurance=0, pmi=0, hoa=0, maintenance=0,
    )


def make_candidate(listing: Listing | None = None, zoning: SchoolZoning = BELLAIRE_FULL,
                   flood: FloodRisk | None = None, **over) -> Candidate:
    return Candidate(listing=listing or make_listing(), zoning=zoning,
                     flood=flood or FloodRisk(fema_zone="X"), **over)


def market(ppsf: float | None = 400.0) -> MarketSnapshot:
    return MarketSnapshot(area_id="bellaire", week_of=date(2026, 11, 9), active_count=5,
                          median_price=1_000_000, median_ppsf=ppsf, median_dom=20,
                          median_rent=5000, price_cut_share=0.1)


def cfg_with(**purchase_or_schools) -> config.SearchConfig:
    raw = CFG.model_dump()
    for key, value in purchase_or_schools.items():
        section, field = key.split("__")
        raw[section][field] = value
    return config.SearchConfig.model_validate(raw)


# ----------------------------------------------------------------- helpers


def test_primary_cost_prefers_plan_market_then_plan_then_first():
    a, b, c = (make_cost(scenario_id=s) for s in ("conventional_20", "plan", "plan_market"))
    assert scoring.primary_cost([a, b, c]) is c
    assert scoring.primary_cost([a, b]) is b
    assert scoring.primary_cost([a]) is a
    assert scoring.primary_cost([]) is None


def test_missing_params_is_a_clear_error():
    raw = CFG.model_dump()
    raw["scoring"]["params"] = None
    bare = config.SearchConfig.model_validate(raw)
    with pytest.raises(ValueError, match="scoring.params"):
        scoring.score(make_candidate(), bare, None, None)


# ----------------------------------------------------------------- schools


def test_schools_part_levels():
    full = make_candidate()
    assert scoring.schools_part(full, CFG) == P.schools_full == 100

    high_only = make_candidate(zoning=BELLAIRE_FULL.model_copy(update={"elementary": "Other El"}))
    assert scoring.schools_part(high_only, CFG) == P.schools_verified_high == 60

    claim = make_candidate(zoning=BELLAIRE_FULL.model_copy(
        update={"confidence": ZoningConfidence.LISTING_CLAIM}))
    assert scoring.schools_part(claim, CFG) == P.schools_claim == 30

    unknown = make_candidate(zoning=SchoolZoning())
    assert scoring.schools_part(unknown, CFG) == P.schools_unknown


def test_schools_part_with_preferred_high():
    raw = CFG.model_dump()
    raw["areas"][0]["target_schools"]["high"].append("Lamar High School")
    raw["areas"][0]["target_schools"]["preferred_high"] = "Bellaire High School"
    cfg = config.SearchConfig.model_validate(raw)
    lamar_target_elem = BELLAIRE_FULL.model_copy(update={"high": "Lamar High School"})
    assert scoring.schools_part(make_candidate(), cfg) == P.schools_full
    assert scoring.schools_part(make_candidate(zoning=lamar_target_elem), cfg) == (
        P.schools_partial
    )
    lamar_other = lamar_target_elem.model_copy(update={"elementary": "Other El"})
    assert scoring.schools_part(make_candidate(zoning=lamar_other), cfg) == (
        P.schools_verified_high
    )


# ------------------------------------------------------------------- value


@pytest.mark.parametrize("ppsf, expected", [
    (400 * 0.85, 100), (400 * 0.80, 100), (400.0, 50), (400 * 1.15, 0), (400 * 1.3, 0),
    (400 * 0.925, 75),
])
def test_value_part_against_median(ppsf, expected):
    c = make_candidate(make_listing(price=round(ppsf * 3000), sqft=3000))
    assert scoring.value_part(c, CFG, market(400.0)) == pytest.approx(expected, abs=0.1)


def test_value_part_neutral_without_market_or_sqft():
    assert scoring.value_part(make_candidate(), CFG, None) == P.value_neutral == 50
    assert scoring.value_part(make_candidate(), CFG, market(None)) == 50
    no_sqft = make_candidate(make_listing(sqft=None))
    assert scoring.value_part(no_sqft, CFG, market()) == 50


def test_value_part_bonuses_and_cap():
    c = make_candidate(make_listing(days_on_market=P.value_dom_days + 1), flags=["price_cut"])
    assert scoring.value_part(c, CFG, None) == 50 + 10 + 5
    at_limit = make_candidate(make_listing(days_on_market=P.value_dom_days))
    assert scoring.value_part(at_limit, CFG, None) == 50
    cheap = make_candidate(make_listing(price=300 * 3000), flags=["price_cut"])
    assert scoring.value_part(cheap, CFG, market(400.0)) == 100


# ------------------------------------------------------------------- flood


def test_flood_part():
    assert scoring.flood_part(make_candidate(flood=FloodRisk(fema_zone="X")), CFG) == 100
    assert scoring.flood_part(make_candidate(flood=FloodRisk(fema_zone="AE")), CFG) == 15
    assert scoring.flood_part(make_candidate(flood=FloodRisk()), CFG) == P.flood_unknown
    flooded = FloodRisk(fema_zone="X", notes=[NOTE_PRIOR_FLOODING])
    assert scoring.flood_part(make_candidate(flood=flooded), CFG) == 40
    claim = FloodRisk(fema_zone="X", notes=[NOTE_CLAIMS_NO_FLOODING])
    assert scoring.flood_part(make_candidate(flood=claim), CFG) == 100


# --------------------------------------------------------------------- fit


def test_fit_part():
    assert scoring.fit_part(make_candidate(), CFG) == 100
    # sqft 10% short of 2400 -> 50 for that criterion; beds and baths meet -> mean
    short = make_candidate(make_listing(sqft=2160))
    assert scoring.fit_part(short, CFG) == pytest.approx((100 + 100 + 50) / 3)
    # beds 3 -> 2 is 33% short: 0 for beds
    tiny = make_candidate(make_listing(beds=2))
    assert scoring.fit_part(tiny, CFG) == pytest.approx(200 / 3)
    unknown = make_candidate(make_listing(baths=None))
    assert scoring.fit_part(unknown, CFG) == pytest.approx((100 + P.fit_unknown + 100) / 3)


# ----------------------------------------------------------- carrying cost


def test_carrying_cost_part():
    profile = make_profile()          # comfortable carry 8,000
    assert scoring.carrying_cost_part(make_candidate(costs=[make_cost()]), CFG, None) == 50
    assert scoring.carrying_cost_part(make_candidate(), CFG, profile) == 50   # no cost
    for total, expected in [(6400, 100), (5000, 100), (8000, 50), (9600, 0), (12000, 0)]:
        c = make_candidate(costs=[make_cost(total)])
        assert scoring.carrying_cost_part(c, CFG, profile) == pytest.approx(expected)
    no_target = make_profile(carrying_costs__total_housing_carry_target_monthly=[])
    assert scoring.carrying_cost_part(make_candidate(costs=[make_cost()]), CFG, no_target) == 50


def test_carrying_cost_uses_larger_of_total_and_plan_basis():
    aff = Affordability(cash_needed=0, cash_available_at_purchase=0, dti_back_end=0.3,
                        plan_basis_monthly=9600, within_comfort=False)
    c = make_candidate(costs=[make_cost(6400)], affordability=aff)
    assert scoring.carrying_cost_part(c, CFG, make_profile()) == 0


# ------------------------------------------------------------ disqualifiers


@pytest.mark.parametrize("flood, tolerance, reason", [
    (FloodRisk(fema_zone="floodway", in_floodway=True), "allow_100yr", "floodway"),
    (FloodRisk(fema_zone="AE"), "x_only", "flood_tolerance"),
    (FloodRisk(fema_zone="X_shaded"), "x_only", "flood_tolerance"),
    (FloodRisk(fema_zone="X_shaded"), "allow_500yr", None),
    (FloodRisk(fema_zone="AE"), "allow_500yr", "flood_tolerance"),
    (FloodRisk(fema_zone="AE"), "allow_100yr", None),
    (FloodRisk(), "x_only", None),        # unknown zone: flagged, not dropped
])
def test_flood_disqualifiers(flood, tolerance, reason):
    profile = make_profile(household__flood_tolerance=tolerance)
    assert scoring.disqualifier(make_candidate(flood=flood), CFG, profile) == reason


def test_no_profile_defaults_to_x_only():
    assert scoring.disqualifier(
        make_candidate(flood=FloodRisk(fema_zone="X_shaded")), CFG, None
    ) == "flood_tolerance"


def test_other_disqualifiers():
    lamar = BELLAIRE_FULL.model_copy(update={"high": "Lamar High School"})
    assert scoring.disqualifier(make_candidate(zoning=lamar), CFG, None) == "non_target_high"
    # an unverified claim of a non-target is not dropped here (it goes to "unverified")
    claim = lamar.model_copy(update={"confidence": ZoningConfidence.LISTING_CLAIM})
    assert scoring.disqualifier(make_candidate(zoning=claim), CFG, None) is None
    words = make_candidate(make_listing(description="Lovely 55+ community"))
    assert scoring.disqualifier(words, CFG, None) == "excluded_keyword"
    pricey = make_candidate(make_listing(price=CFG.purchase.stretch_max + 1))
    assert scoring.disqualifier(pricey, CFG, None) == "over_stretch_max"
    elsewhere = make_candidate(make_listing(zip_code="77002"), zoning=SchoolZoning())
    assert scoring.disqualifier(elsewhere, CFG, None) == "outside_areas"
    aff = Affordability(cash_needed=0, cash_available_at_purchase=0, dti_back_end=0.3,
                        plan_basis_monthly=0, within_comfort=False, over_hard_ceiling=True)
    assert scoring.disqualifier(make_candidate(affordability=aff), CFG, None) == (
        "over_hard_ceiling"
    )


def test_score_returns_none_when_disqualified():
    c = make_candidate(flood=FloodRisk(fema_zone="floodway", in_floodway=True))
    assert scoring.score(c, CFG, None, None) is None


# ------------------------------------------------------------------- score


def test_score_is_weighted_sum_of_parts():
    c = make_candidate(costs=[make_cost()])
    total = scoring.score(c, CFG, market(1_000_000 / 3000), None)
    parts = c.score_parts
    assert set(parts) == set(scoring.PART_NAMES)
    assert all(0 <= v <= 100 for v in parts.values())
    w = CFG.scoring.weights
    assert total == pytest.approx(sum(w[k] * parts[k] for k in w), abs=0.05)
    assert c.score == total
    # schools 100, value 50 (at median), flood 100, fit 100, carrying 50 (no profile)
    assert total == pytest.approx(0.30 * 100 + 0.25 * 50 + 0.20 * 100 + 0.15 * 100 + 0.10 * 50)


def test_unverified_scores_with_unverified_schools_part():
    claim = BELLAIRE_FULL.model_copy(update={"confidence": ZoningConfidence.LISTING_CLAIM})
    c = make_candidate(zoning=claim)
    scoring.score(c, CFG, None, None)
    assert c.score_parts["schools"] == 30


# ------------------------------------------------------------------- flags


def test_flags():
    c = make_candidate(
        make_listing(price=CFG.purchase.price_max + 1, year_built=1960),
        zoning=SchoolZoning(),
        flood=FloodRisk(fema_zone="X_shaded"),
        costs=[make_cost(jumbo=True)],
        flags=["new", "price_cut"],
    )
    got = scoring.flags(c, CFG)
    assert got[:2] == ["new", "price_cut"]
    assert {"stretch", "zoning_unverified", "jumbo", "flood_500yr", "lot_value"} <= set(got)
    assert "flood_unknown" not in got


def test_flags_quiet_listing_and_unknown_flood():
    assert scoring.flags(make_candidate(costs=[make_cost()]), CFG) == []
    assert scoring.flags(make_candidate(flood=FloodRisk()), CFG) == ["flood_unknown"]


def test_rental_price_is_not_a_stretch():
    rent = make_candidate(make_listing(kind=ListingKind.RENT, price=5000, year_built=1960))
    assert scoring.flags(rent, CFG) == []
