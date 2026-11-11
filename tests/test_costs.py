"""WP7: monthly cost of ownership, docs/FINANCE_MODEL.md §1.

Assumptions are built here with the spec's example values, not read from
config/assumptions.yaml, so the required figures stay fixed as the live
assumptions change.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from home_search.config import Assumptions, LoanScenario
from home_search.finance.costs import (
    all_scenarios,
    default_scenarios,
    loan_cost,
    monthly_payment,
)
from home_search.models import FloodRisk, Listing, ListingKind


def make_assumptions(**over) -> Assumptions:
    """Assumptions with the §1 worked-example values. Keyword overrides use
    dotted-path names with `__`, e.g. mortgage__jumbo_rate_spread=0.0025."""
    raw = {
        "mortgage": {
            "rate_30yr_fixed": 0.065,
            "jumbo_rate_spread": 0.0,
            "conforming_limit": 832_750,
            "term_years": 30,
            "scenarios": [
                {"id": "conventional_20", "down_pct": 0.20, "pmi": False},
                {"id": "conventional_10", "down_pct": 0.10, "pmi": True},
                {"id": "physician_0", "down_pct": 0.0, "pmi": False, "rate_spread": 0.0025},
            ],
            "pmi_annual_pct": 0.005,
        },
        "property_tax": {
            "rate_by_area": {"memorial": 0.021, "bellaire": 0.020},
            "school_share_by_area": {"memorial": 0.50, "bellaire": 0.40},
            "homestead_exemption_school": 140_000,
            "homestead_cap_annual": 0.10,
        },
        "insurance": {
            "homeowners_pct_of_value": 0.0085,
            "flood_annual_by_zone": {"X": 900, "X_shaded": 1800, "AE": 4500, "floodway": None},
        },
        "ownership": {
            "maintenance_pct_of_value": 0.01,
            "closing_cost_buy_pct": 0.02,
            "closing_cost_sell_pct": 0.065,
            "home_appreciation": 0.035,
            "hoa_monthly_default": 0,
        },
        "renting": {"rent_growth": 0.035, "fallback_price_to_annual_rent": 22},
        "investing": {"return_nominal": 0.065, "inflation": 0.025},
        "tax": {"mortgage_interest_debt_cap": 750_000, "salt_cap": 40_000,
                "standard_deduction_married_joint": 30_000,
                "salt_phaseout_start": 500_000, "salt_phaseout_rate": 0.30,
                "salt_floor": 10_000, "first_tax_year": 2027,
                "salt_revert_tax_year": 2030, "salt_cap_after_revert": 10_000},
        "horizon_years": 12,
        "rent_vs_buy_close_call_pct": 0.02,
        "sensitivity": {"horizons": [10], "appreciation": [0.035], "rate": [0.0]},
    }
    for key, value in over.items():
        *path, leaf = key.split("__")
        node = raw
        for p in path:
            node = node[p]
        node[leaf] = value
    return Assumptions.model_validate(raw)


def make_listing(price: int, hoa: int | None = None) -> Listing:
    return Listing(
        source="test", source_id="1", url="https://example.invalid/1", kind=ListingKind.SALE,
        address="100 Example St", city="Houston", zip="77024", price=price,
        hoa_monthly=hoa, fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


def scenario(a: Assumptions, sid: str) -> LoanScenario:
    return next(s for s in a.mortgage.scenarios if s.id == sid)


X = FloodRisk(fema_zone="X")


# ------------------------------------------------------------- amortization


def test_monthly_payment_reference_values():
    # $800k at 6.5% / 30y, and the classic $100k at 6% / 30y = 599.55
    assert monthly_payment(800_000, 0.065, 30) == pytest.approx(5056.54, abs=0.005)
    assert monthly_payment(100_000, 0.06, 30) == pytest.approx(599.55, abs=0.005)


def test_monthly_payment_zero_rate_and_zero_principal():
    assert monthly_payment(360_000, 0.0, 30) == pytest.approx(1000.0)
    assert monthly_payment(0, 0.065, 30) == 0.0


# ------------------------------------------------------ worked example §1


def test_worked_example_to_the_cent():
    a = make_assumptions()
    c = loan_cost(make_listing(1_000_000), "memorial", X, scenario(a, "conventional_20"), a)
    assert c.loan_amount == 800_000
    assert c.down_payment == 200_000
    assert not c.is_jumbo
    assert c.rate == pytest.approx(0.065)
    assert c.principal_interest == pytest.approx(5056.54, abs=0.001)
    assert c.property_tax == pytest.approx(1627.50, abs=0.001)
    assert c.homeowners_insurance == pytest.approx(708.33, abs=0.001)
    assert c.flood_insurance == pytest.approx(75.00, abs=0.001)
    assert c.maintenance == pytest.approx(833.33, abs=0.001)
    assert c.pmi == 0
    assert c.hoa == 0
    assert c.total_monthly == pytest.approx(8300.70, abs=0.001)


# ------------------------------------------------------------------ jumbo


def test_jumbo_flips_at_conforming_limit():
    a = make_assumptions(mortgage__jumbo_rate_spread=0.0025)
    s20 = scenario(a, "conventional_20")
    limit = a.mortgage.conforming_limit
    at_limit = int(limit / (1 - s20.down_pct))          # loan exactly at the limit
    below = loan_cost(make_listing(at_limit), "memorial", X, s20, a)
    above = loan_cost(make_listing(at_limit + 100), "memorial", X, s20, a)
    assert below.loan_amount <= limit and not below.is_jumbo
    assert below.rate == pytest.approx(0.065)
    assert above.loan_amount > limit and above.is_jumbo
    assert above.rate == pytest.approx(0.0675)


def test_scenario_rate_spread_is_added():
    a = make_assumptions()
    c = loan_cost(make_listing(500_000), "memorial", X, scenario(a, "physician_0"), a)
    assert c.rate == pytest.approx(0.0675)
    assert c.loan_amount == 500_000 and c.down_payment == 0


# --------------------------------------------------------- property tax


def test_homestead_exemption_reduces_school_portion_only():
    a = make_assumptions()
    s = scenario(a, "conventional_20")
    c = loan_cost(make_listing(1_000_000), "memorial", X, s, a)
    no_exempt = make_assumptions(property_tax__homestead_exemption_school=0)
    c0 = loan_cost(make_listing(1_000_000), "memorial", X, s, no_exempt)
    # Saving is the exemption times the school part of the rate only:
    # 140,000 x (0.021 x 0.50) / 12 = 122.50, not 140,000 x 0.021 / 12 = 245.00.
    assert c0.property_tax - c.property_tax == pytest.approx(122.50, abs=0.01)
    all_school = make_assumptions(property_tax__school_share_by_area={"memorial": 1.0})
    c1 = loan_cost(make_listing(1_000_000), "memorial", X, s, all_school)
    assert c0.property_tax - c1.property_tax == pytest.approx(245.00, abs=0.01)


def test_tax_uses_area_rate():
    a = make_assumptions(property_tax__homestead_exemption_school=0)
    s = scenario(a, "conventional_20")
    m = loan_cost(make_listing(1_200_000), "memorial", X, s, a)
    b = loan_cost(make_listing(1_200_000), "bellaire", X, s, a)
    assert m.property_tax == pytest.approx(2100.00)
    assert b.property_tax == pytest.approx(2000.00)


# ------------------------------------------------------------------- PMI


def test_pmi_only_when_scenario_says_so():
    a = make_assumptions()
    listing = make_listing(900_000)
    c10 = loan_cost(listing, "memorial", X, scenario(a, "conventional_10"), a)
    c20 = loan_cost(listing, "memorial", X, scenario(a, "conventional_20"), a)
    c0 = loan_cost(listing, "memorial", X, scenario(a, "physician_0"), a)
    assert c10.pmi == pytest.approx(810_000 * 0.005 / 12, abs=0.005)
    assert c20.pmi == 0
    assert c0.pmi == 0


# ------------------------------------------------------- flood and HOA


@pytest.mark.parametrize(
    ("flood", "monthly"),
    [
        (FloodRisk(fema_zone="X"), 75.00),
        (FloodRisk(fema_zone="X_shaded"), 150.00),
        (FloodRisk(fema_zone="AE"), 375.00),
        (FloodRisk(fema_zone=None), 150.00),          # unknown priced as X_shaded
        (FloodRisk(fema_zone="VE"), 150.00),          # unpriced zone too
        (FloodRisk(fema_zone="floodway", in_floodway=True), 375.00),  # never understated
    ],
)
def test_flood_premium_by_zone(flood, monthly):
    a = make_assumptions()
    c = loan_cost(make_listing(1_000_000), "memorial", flood, scenario(a, "conventional_20"), a)
    assert c.flood_insurance == pytest.approx(monthly)


def test_hoa_listing_value_else_default():
    a = make_assumptions(ownership__hoa_monthly_default=50)
    s = scenario(a, "conventional_20")
    assert loan_cost(make_listing(800_000, hoa=120), "memorial", X, s, a).hoa == 120
    assert loan_cost(make_listing(800_000), "memorial", X, s, a).hoa == 50
    assert loan_cost(make_listing(800_000, hoa=0), "memorial", X, s, a).hoa == 0


# --------------------------------------------------------- all_scenarios


def test_all_scenarios_defaults_exclude_physician():
    a = make_assumptions()
    ids = [c.scenario_id for c in all_scenarios(make_listing(1_000_000), "memorial", X, a)]
    assert ids == ["conventional_20", "conventional_10"]
    assert [s.id for s in default_scenarios(a)] == ids


def test_all_scenarios_explicit_list_in_order():
    a = make_assumptions()
    chosen = [scenario(a, "physician_0"), scenario(a, "conventional_20")]
    costs = all_scenarios(make_listing(1_000_000), "memorial", X, a, scenarios=chosen)
    assert [c.scenario_id for c in costs] == ["physician_0", "conventional_20"]
