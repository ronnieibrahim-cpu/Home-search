"""WP8: rent vs buy, docs/FINANCE_MODEL.md §3. Synthetic data only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_affordability import make_profile
from test_costs import X, make_assumptions, make_listing

from home_search.finance.affordability import costs_for
from home_search.finance.rent_vs_buy import (
    FALLBACK_SOURCE,
    analyze,
    portfolio_view,
    rent_equivalent,
    salt_cap_for,
    sensitivity_key,
)
from home_search.models import Listing, ListingKind, ListingStatus, LoanCost, RentVsBuy


def make_rental(rent: int, sqft: int | None = 3000, beds: float | None = 4, i: int = 0,
                status: ListingStatus = ListingStatus.ACTIVE) -> Listing:
    return Listing(
        source="test", source_id=f"r{i}", url=f"https://example.invalid/r{i}",
        kind=ListingKind.RENT, status=status, address=f"{i} Rental Ln", city="Houston",
        zip="77024", price=rent, sqft=sqft, beds=beds,
        fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


def sized(price: int, sqft: int | None = 3000, beds: float | None = 4) -> Listing:
    return make_listing(price).model_copy(update={"sqft": sqft, "beds": beds})


def simple_cost() -> LoanCost:
    """Round numbers: $900k, 20% down, $720k at 0% over 30 years = $2,000/month."""
    return LoanCost(
        scenario_id="conventional_20", down_payment=180_000, loan_amount=720_000, rate=0.0,
        is_jumbo=False, principal_interest=2_000, property_tax=1_500,
        homeowners_insurance=600, flood_insurance=75, pmi=0, hoa=0, maintenance=750,
    )


def zero_growth(**over):
    base = {
        "mortgage__rate_30yr_fixed": 0.0,
        "ownership__home_appreciation": 0.0,
        "renting__rent_growth": 0.0,
        "renting__renters_insurance_annual": 1_200,
        "investing__return_nominal": 0.0,
        "investing__inflation": 0.0,
        "horizon_years": 10,
        "sensitivity": {"horizons": [5, 10, 15], "appreciation": [0.0], "rate": [0.0]},
    }
    base.update(over)
    return make_assumptions(**base)


def market_cost(price: int, a, profile=None) -> LoanCost:
    costs = costs_for(sized(price), "memorial", X, profile, a)
    return next(c for c in costs if c.scenario_id in ("plan_market", "conventional_20"))


# ----------------------------------------------------------- rent level


def test_fallback_rent_used_and_labelled_when_no_comps():
    a = make_assumptions()
    listing = sized(1_100_000)
    rent, source = rent_equivalent(listing, [], a)
    assert source == FALLBACK_SOURCE == "fallback_ratio"
    assert rent == pytest.approx(1_100_000 / 22 / 12)
    # Rentals exist but none is comparable: still the fallback.
    far = [make_rental(9_000, sqft=5000), make_rental(4_000, beds=2, i=1),
           make_rental(5_000, status=ListingStatus.OFF_MARKET, i=2)]
    assert rent_equivalent(listing, far, a)[1] == FALLBACK_SOURCE
    r = analyze(listing, market_cost(1_100_000, a), [], None, a)
    assert r.rent_source == "fallback_ratio"
    assert r.rent_equivalent_monthly == pytest.approx(4166.67, abs=0.01)
    assert "fallback price-to-rent ratio" in r.rationale[0]


def test_comp_median_within_size_window():
    a = make_assumptions()
    listing = sized(1_000_000, sqft=3000, beds=4)
    rentals = [
        make_rental(5_000, sqft=2400, beds=3, i=1),     # -20%, -1 bed: in
        make_rental(6_000, sqft=3600, beds=5, i=2),     # +20%, +1 bed: in
        make_rental(5_500, sqft=3000, beds=4, i=3),     # in
        make_rental(9_000, sqft=3700, beds=4, i=4),     # sqft out
        make_rental(3_000, sqft=3000, beds=2, i=5),     # beds out
        make_rental(4_000, sqft=None, beds=4, i=6),     # no sqft: out
    ]
    rent, source = rent_equivalent(listing, rentals, a)
    assert rent == 5_500
    assert source == "comp:3 rentals"
    # A listing with no size data has nothing to compare against.
    assert rent_equivalent(sized(1_000_000, None, None), rentals, a)[1] == FALLBACK_SOURCE


# ------------------------------------------------------ hand-computed case


def test_zero_growth_zero_return_matches_hand_computation():
    """No appreciation, return, inflation or rent growth; 0% loan; no profile.

    Outlay at month 0: 180,000 down + 2% of 900,000 closing = 198,000.
    Buyer pays 2,000 + 1,500 + 600 + 75 + 750 = 4,925/month; renter pays
    4,000 rent + 100 renters insurance = 4,100, so the renter invests 825/month.
    After 10 years: balance 720,000 - 120 x 2,000 = 480,000; buyer equity
    900,000 x (1 - 0.065) - 480,000 = 361,500; renter 198,000 + 120 x 825
    = 297,000. NPV = 64,500, above 2% of price (18,000): buy.
    Year y: 841,500 - (720,000 - 24,000 y) - 198,000 - 9,900 y = -76,500
    + 14,100 y, positive from y = 6.
    """
    a = zero_growth()
    r = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], None, a)
    assert r.rent_equivalent_monthly == 4_000
    assert r.rent_source == "comp:1 rentals"
    assert r.npv_buy_minus_rent == pytest.approx(64_500, abs=0.01)
    assert r.verdict == "buy"
    assert r.breakeven_year == 6
    assert r.horizon_years == 10
    assert r.sensitivity[sensitivity_key(5, 0.0, 0.0)] == pytest.approx(-76_500 + 5 * 14_100)
    assert r.sensitivity["h=15,app=0,rate=+0"] == pytest.approx(-76_500 + 15 * 14_100)
    # Discounting divides by (1 + inflation)^H.
    infl = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], None,
                   zero_growth(investing__inflation=0.02))
    assert infl.npv_buy_minus_rent == pytest.approx(64_500 / 1.02 ** 10, abs=0.01)


def test_rationale_cites_the_actual_numbers():
    r = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], None, zero_growth())
    text = " ".join(r.rationale)
    assert 2 <= len(r.rationale) <= 4
    assert "$900,000" in text and "$64,500" in text and "10 years" in text
    assert "$4,000/month" in text
    # Year-1 unrecoverable cost: interest 0 + tax 1,500 + insurance 675 + maintenance 750.
    assert "$2,925/month" in text and "$4,100/month" in text
    assert "year 6" in text
    assert "no profile is loaded" in text


# ----------------------------------------------------------------- verdicts


def test_verdict_flips_to_rent_as_horizon_shrinks():
    a = make_assumptions(ownership__home_appreciation=0.045)
    cost = market_cost(1_000_000, a)
    rent = [make_rental(7_000)]
    verdicts = {
        h: analyze(sized(1_000_000), cost, rent, None, a.model_copy(update={"horizon_years": h}))
        for h in (1, 2, 3, 5, 10, 25)
    }
    assert verdicts[1].verdict == "rent"
    assert verdicts[25].verdict == "buy"
    npvs = [verdicts[h].npv_buy_minus_rent for h in sorted(verdicts)]
    assert npvs == sorted(npvs)
    order = ["rent", "close_call", "buy"]
    ranks = [order.index(verdicts[h].verdict) for h in sorted(verdicts)]
    assert ranks == sorted(ranks)


def test_close_call_band_is_two_percent_of_price():
    a = zero_growth()
    # Rent chosen so the 10-year NPV is exactly 0: each $1 of monthly rent adds
    # $120 to the renter's cost; 64,500 / 120 = 537.5.
    r = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], None, a)
    lo = analyze(sized(900_000), simple_cost(), [make_rental(3_463)], None, a)
    assert r.verdict == "buy"
    assert lo.npv_buy_minus_rent == pytest.approx(64_500 - 537 * 120)
    assert lo.verdict == "close_call"
    wide = zero_growth(rent_vs_buy_close_call_pct=0.10)
    assert analyze(sized(900_000), simple_cost(), [make_rental(4_000)], None, wide).verdict == (
        "close_call"
    )


def test_higher_appreciation_never_lowers_npv():
    apps = [-0.02, 0.0, 0.01, 0.02, 0.035, 0.05, 0.08]
    for horizon in (3, 7, 12, 20):
        npvs = []
        for g in apps:
            a = make_assumptions(ownership__home_appreciation=g, horizon_years=horizon)
            cost = market_cost(1_000_000, a)
            npvs.append(analyze(sized(1_000_000), cost, [], None, a).npv_buy_minus_rent)
        assert npvs == sorted(npvs), horizon


def test_sensitivity_grid_keys_and_monotonic_in_appreciation_and_rate():
    a = make_assumptions(sensitivity={"horizons": [5, 10], "appreciation": [0.02, 0.05],
                                      "rate": [-0.01, 0.0, 0.01]})
    r = analyze(sized(1_000_000), market_cost(1_000_000, a), [], None, a)
    assert len(r.sensitivity) == 2 * 2 * 3
    assert "h=10,app=0.02,rate=+0.01" in r.sensitivity
    assert "h=5,app=0.05,rate=-0.01" in r.sensitivity
    for h in (5, 10):
        for dr in ("-0.01", "+0", "+0.01"):
            assert r.sensitivity[f"h={h},app=0.05,rate={dr}"] >= (
                r.sensitivity[f"h={h},app=0.02,rate={dr}"]
            )
        assert r.sensitivity[f"h={h},app=0.02,rate=-0.01"] > (
            r.sensitivity[f"h={h},app=0.02,rate=+0.01"]
        )


# ---------------------------------------------------------------- tax effect


def test_tax_effect_needs_profile_and_marginal_rate():
    a = make_assumptions()
    price = 1_100_000
    rent = [make_rental(5_500)]
    no_rate = make_profile()
    with_rate = make_profile(household__marginal_federal_rate=0.35)
    cost = market_cost(price, a, no_rate)
    base = analyze(sized(price), cost, rent, no_rate, a)
    taxed = analyze(sized(price), cost, rent, with_rate, a)
    assert "no marginal federal rate" in " ".join(base.rationale)
    assert taxed.npv_buy_minus_rent > base.npv_buy_minus_rent
    assert "federal tax in year 1" in " ".join(taxed.rationale)
    # A standard deduction above any itemized total removes the benefit.
    huge = make_assumptions(tax__standard_deduction_married_joint=10_000_000)
    assert analyze(sized(price), cost, rent, with_rate, huge).npv_buy_minus_rent == (
        pytest.approx(analyze(sized(price), cost, rent, no_rate, huge).npv_buy_minus_rent)
    )


def test_salt_cap_phase_down_floor_and_reversion():
    a = make_assumptions()       # cap 40,000; phase-out from 500,000 at 30%; floor 10,000
    assert salt_cap_for(2027, 300_000, a) == 40_000
    assert salt_cap_for(2027, 500_000, a) == 40_000
    assert salt_cap_for(2027, 550_000, a) == pytest.approx(25_000)
    assert salt_cap_for(2029, 600_000, a) == 10_000           # 40,000 - 30,000 = floor
    assert salt_cap_for(2027, 2_000_000, a) == 10_000         # floor holds
    assert salt_cap_for(2030, 300_000, a) == 10_000           # reverted
    assert salt_cap_for(2035, 300_000, a) == 10_000


def _year1_saving(r: RentVsBuy) -> float:
    sentence = next(s for s in r.rationale if "federal tax in year 1" in s)
    return float(sentence.split("about $")[1].split()[0].replace(",", ""))


def test_phase_down_reduces_benefit_for_high_income_only():
    """$1.1M: year-1 property tax ~23,000, so a 40,000 cap does not bind but a
    phased-down cap does."""
    a = make_assumptions()
    price, rent = 1_100_000, [make_rental(5_500)]

    def run(monthly_income: float, **over) -> RentVsBuy:
        p = make_profile(household__marginal_federal_rate=0.35,
                         income__gross_monthly_for_lender=monthly_income)
        aa = make_assumptions(**over) if over else a
        return analyze(sized(price), market_cost(price, a, p), rent, p, aa)

    low, low_no_phase = run(30_000), run(30_000, tax__salt_phaseout_start=10_000_000)
    assert low.npv_buy_minus_rent == pytest.approx(low_no_phase.npv_buy_minus_rent)
    high, high_no_phase = run(80_000), run(80_000, tax__salt_phaseout_start=10_000_000)
    assert high.npv_buy_minus_rent < high_no_phase.npv_buy_minus_rent
    assert _year1_saving(high) < _year1_saving(high_no_phase)
    # Beyond the floor more income changes nothing.
    assert run(200_000).npv_buy_minus_rent == pytest.approx(run(400_000).npv_buy_minus_rent)


def test_benefit_drops_in_the_revert_year():
    """Zero growth and a 0% loan isolate SALT: itemized = property tax only.
    18,000 tax vs standard deduction 0 -> benefit = 0.35 x min(18,000, cap)."""
    a = zero_growth(tax__standard_deduction_married_joint=0, tax__first_tax_year=2027,
                    tax__salt_revert_tax_year=2029,
                    sensitivity={"horizons": [1, 2, 3], "appreciation": [0.0], "rate": [0.0]})
    p = make_profile(household__marginal_federal_rate=0.35,
                     income__gross_monthly_for_lender=10_000)
    no_tax = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], None, a)
    taxed = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], p, a)
    gain = {h: taxed.sensitivity[sensitivity_key(h, 0.0, 0.0)]
            - no_tax.sensitivity[sensitivity_key(h, 0.0, 0.0)] for h in (1, 2, 3)}
    assert gain[1] == pytest.approx(0.35 * 18_000)              # 2027: full 18,000
    assert gain[2] - gain[1] == pytest.approx(0.35 * 18_000)    # 2028
    assert gain[3] - gain[2] == pytest.approx(0.35 * 10_000)    # 2029: reverted cap
    assert "$6,300 in federal tax in year 1" in " ".join(taxed.rationale)


def test_profile_closing_share_raises_the_month0_outlay():
    a = zero_growth()
    p_lo = make_profile(financing__closing_costs_pct=0.02)
    p_hi = make_profile(financing__closing_costs_pct=0.03)
    lo = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], p_lo, a)
    hi = analyze(sized(900_000), simple_cost(), [make_rental(4_000)], p_hi, a)
    # One more point of closing costs = 9,000 more month-0 cash, zero return.
    assert lo.npv_buy_minus_rent - hi.npv_buy_minus_rent == pytest.approx(9_000)


# ----------------------------------------------------------------- portfolio


def _rvb(npv: float, verdict: str, be: int | None, grid: dict[str, float]) -> RentVsBuy:
    return RentVsBuy(horizon_years=10, rent_equivalent_monthly=5_000, rent_source="x",
                     npv_buy_minus_rent=npv, breakeven_year=be, verdict=verdict,
                     rationale=["a", "b"], sensitivity=grid)


def test_portfolio_view_headline_and_swing_factor():
    # Appreciation flips the sign at both horizons; rate never does.
    grid = {
        "h=5,app=0.02,rate=+0": -50_000, "h=5,app=0.05,rate=+0": 10_000,
        "h=10,app=0.02,rate=+0": -5_000, "h=10,app=0.05,rate=+0": 80_000,
        "h=5,app=0.02,rate=+0.01": -60_000, "h=5,app=0.05,rate=+0.01": 5_000,
        "h=10,app=0.02,rate=+0.01": -9_000, "h=10,app=0.05,rate=+0.01": 70_000,
    }
    results = [_rvb(40_000, "buy", 6, grid), _rvb(-30_000, "rent", None, grid),
               _rvb(100_000, "buy", 4, grid)]
    v = portfolio_view(results)
    assert v["count"] == 3
    assert v["verdicts"] == {"buy": 2, "close_call": 0, "rent": 1}
    assert v["buy_share"] == pytest.approx(2 / 3)
    assert v["median_breakeven_year"] == 5
    assert v["never_break_even"] == 1
    assert v["median_npv"] == 40_000
    assert v["swing_factor"]["dimension"] == "app"
    assert v["swing_factor"]["label"] == "home appreciation"
    assert v["swing_factor"]["flip_share"] == 1.0


def test_portfolio_view_empty():
    v = portfolio_view([])
    assert v["count"] == 0 and v["buy_share"] is None and v["swing_factor"] is None
