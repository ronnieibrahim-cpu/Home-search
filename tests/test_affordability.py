"""WP7: affordability, docs/FINANCE_MODEL.md §2. Synthetic profiles only."""

from __future__ import annotations

from datetime import date

import pytest
from test_costs import X, make_assumptions, make_listing

from home_search.config import Profile
from home_search.finance.affordability import (
    assess,
    cash_at,
    costs_for,
    effective_assumptions,
    max_price,
    plan_basis_monthly,
    plan_rate_bracket,
    plan_scenario,
    reserve_months,
    scenarios_for,
    target_close_date,
    whole_months_between,
)
from home_search.finance.costs import loan_cost

GENERATED = date(2026, 11, 11)
CLOSE = date(2027, 6, 1)            # 6 whole months after GENERATED


def make_profile(**over) -> Profile:
    """A made-up household. Keyword overrides use `section__field`."""
    raw = {
        "meta": {"generated": GENERATED.isoformat(), "target_close_date": CLOSE.isoformat()},
        "income": {"gross_monthly_for_lender": 40_000},
        "cash_for_purchase": {"balance": 300_000, "monthly_addition": 5_000,
                              "keep_after_closing": 50_000},
        "other_assets": {"retirement": 120_000},
        "financing": {"down_payment_pct": 0.15, "rate_assumption": 0.065,
                      "rate_range": [0.06, 0.07]},
        "carrying_costs": {"property_tax_rate_modeled": 0.021,
                           "insurance_plus_maintenance_annual": 15_000,
                           "total_housing_carry_target_monthly": [8_000, 9_500]},
        "monthly_budget_after_purchase": {"hard_ceiling_total_housing": 10_500},
        "household": {"monthly_non_housing_debt": 1_000},
    }
    for key, value in over.items():
        section, field = key.split("__")
        raw.setdefault(section, {})[field] = value
    return Profile.model_validate(raw)


def plan_cost(price: int, profile: Profile, a, flood=X, area="memorial"):
    ea = effective_assumptions(profile, a)
    return loan_cost(make_listing(price), area, flood, plan_scenario(profile, a), ea)


# --------------------------------------------------------------- scenarios


def test_plan_scenarios_first_and_physician_opt_in():
    a = make_assumptions()
    p = make_profile()
    assert [s.id for s in scenarios_for(p, a)] == [
        "plan", "plan_market", "conventional_20", "conventional_10",
    ]
    p_doc = make_profile(financing__physician_loan_programs="consider")
    assert [s.id for s in scenarios_for(p_doc, a)] == [
        "plan", "plan_market", "conventional_20", "conventional_10", "physician_0",
    ]
    assert [s.id for s in scenarios_for(None, a)] == ["conventional_20", "conventional_10"]
    for s in scenarios_for(p, a)[:2]:
        assert s.down_pct == 0.15 and s.pmi is False


def test_no_planning_rate_means_plan_only_at_market():
    a = make_assumptions(mortgage__rate_30yr_fixed=0.0718)
    p = make_profile(financing__rate_assumption=None)
    assert [s.id for s in scenarios_for(p, a)] == ["plan", "conventional_20", "conventional_10"]
    costs = costs_for(make_listing(1_000_000), "memorial", X, p, a)
    assert costs[0].rate == pytest.approx(0.0718)


def test_plan_at_planning_rate_others_at_market_rate():
    a = make_assumptions(mortgage__rate_30yr_fixed=0.0718)
    p = make_profile(financing__physician_loan_programs="consider")
    costs = {c.scenario_id: c
             for c in costs_for(make_listing(800_000), "memorial", X, p, a)}  # conforming
    assert costs["plan"].rate == pytest.approx(0.065)
    assert costs["plan_market"].rate == pytest.approx(0.0718)
    assert costs["conventional_20"].rate == pytest.approx(0.0718)
    assert costs["conventional_10"].rate == pytest.approx(0.0718)
    assert costs["physician_0"].rate == pytest.approx(0.0718 + 0.0025)
    assert costs["plan"].loan_amount == costs["plan_market"].loan_amount == 680_000
    assert costs["plan"].principal_interest < costs["plan_market"].principal_interest
    # Other scenarios do not depend on the profile's rate at all.
    no_profile = {c.scenario_id: c
                  for c in costs_for(make_listing(800_000), "memorial", X, None, a)}
    for sid in ("conventional_20", "conventional_10"):
        assert costs[sid] == no_profile[sid]
    # The market rate itself is never overridden.
    assert effective_assumptions(p, a).mortgage.rate_30yr_fixed == pytest.approx(0.0718)


def test_flags_evaluated_per_scenario_rate():
    a = make_assumptions(mortgage__rate_30yr_fixed=0.0818)
    p = make_profile()
    price = 1_150_000
    costs = {c.scenario_id: c
             for c in costs_for(make_listing(price), "memorial", X, p, a)}
    plan = assess(costs["plan"], price, p, a, CLOSE)
    market = assess(costs["plan_market"], price, p, a, CLOSE)
    assert plan.plan_basis_monthly < market.plan_basis_monthly
    assert plan.cash_needed == market.cash_needed
    assert not plan.over_hard_ceiling and market.over_hard_ceiling


# ----------------------------------------------------- required test §2


def test_plan_scenario_required_figures():
    # Assumptions rate differs from the profile's 6.5% to prove the override.
    a = make_assumptions(mortgage__rate_30yr_fixed=0.0718)
    p = make_profile()
    cost = plan_cost(1_100_000, p, a)
    assert cost.scenario_id == "plan"
    assert cost.loan_amount == 935_000
    assert cost.rate == pytest.approx(0.065)
    assert cost.is_jumbo                         # 1.1M > 832,750 / 0.85
    assert cost.pmi == 0
    assert cost.principal_interest == pytest.approx(5909.84, abs=0.001)
    assert plan_basis_monthly(cost, 1_100_000, p) == pytest.approx(9084.84, abs=0.001)

    r = assess(cost, 1_100_000, p, a, CLOSE)
    assert r.plan_basis_monthly == pytest.approx(9084.84, abs=0.001)
    # Itemized §1 figure, shown alongside: P&I + tax on (P - 140k x 0.5) at 2.1%
    # + homeowners 0.85% + X flood 900/yr + maintenance 1%.
    expected_total = 5909.84 + 1802.50 + 779.17 + 75.00 + 916.67
    assert cost.total_monthly == pytest.approx(expected_total, abs=0.001)


def test_jumbo_boundary_at_15_percent_down():
    a = make_assumptions()
    p = make_profile()
    edge = int(a.mortgage.conforming_limit / 0.85)
    assert not plan_cost(edge, p, a).is_jumbo
    assert plan_cost(edge + 100, p, a).is_jumbo


def test_rate_bracket():
    a = make_assumptions()
    br = plan_rate_bracket(1_000_000, make_profile(), a)
    assert set(br) == {0.06, 0.07}
    assert br[0.06] < br[0.07]
    assert br[0.06] == pytest.approx(850_000 * 0.005 / (1 - 1.005 ** -360), abs=0.01)


# -------------------------------------------------------------------- cash


def test_whole_months():
    assert whole_months_between(GENERATED, CLOSE) == 6
    assert whole_months_between(date(2026, 11, 1), date(2027, 6, 1)) == 7
    assert whole_months_between(CLOSE, GENERATED) == 0


def test_target_close_date_default():
    p = make_profile(meta__target_close_date=None)
    assert target_close_date(p, 2027) == date(2027, 6, 1)
    assert target_close_date(make_profile(), 2030) == CLOSE


def test_cash_at_grows_and_keeps_buffer():
    a = make_assumptions()
    assert cash_at(make_profile(), CLOSE, a) == pytest.approx(300_000 + 5_000 * 6 - 50_000)
    no_date = make_profile(meta__generated=None)
    assert cash_at(no_date, CLOSE, a) == pytest.approx(250_000)


def test_emergency_buffer_is_never_spent():
    a = make_assumptions()
    # 280k usable; balance + additions is 330k. At 1.6M, cash needed is
    # 1.6M x (0.15 + 0.02) = 272k: fits. At 1.7M it is 289k: fits the gross
    # balance but would dip into the buffer, so it must be cash-short.
    p = make_profile(
        carrying_costs__total_housing_carry_target_monthly=[],
        monthly_budget_after_purchase__hard_ceiling_total_housing=None,
    )
    ok = assess(plan_cost(1_600_000, p, a), 1_600_000, p, a, CLOSE)
    assert ok.within_comfort and ok.cash_needed == pytest.approx(272_000)
    short = assess(plan_cost(1_700_000, p, a), 1_700_000, p, a, CLOSE)
    assert short.cash_needed == pytest.approx(289_000)
    assert short.cash_needed < 330_000
    assert short.cash_available_at_purchase == pytest.approx(280_000)
    assert not short.within_comfort
    assert any("$9,000" in b and "$1,647,000" in b for b in short.blockers)
    # max_price never lets the buffer go: 280k / 0.17 = 1,647,058 -> 1,647,000
    assert max_price(p, "memorial", a, CLOSE)["plan"] == 1_647_000


def test_closing_cost_override_from_profile():
    a = make_assumptions()
    p = make_profile(financing__closing_costs_pct=0.03)
    r = assess(plan_cost(1_000_000, p, a), 1_000_000, p, a, CLOSE)
    assert r.cash_needed == pytest.approx(180_000)
    r0 = assess(plan_cost(1_000_000, make_profile(), a), 1_000_000, make_profile(), a, CLOSE)
    assert r0.cash_needed == pytest.approx(170_000)


def test_reserve_months_reported_not_added():
    a = make_assumptions()
    p = make_profile()
    cost = plan_cost(1_000_000, p, a)
    assert reserve_months(p, cost.total_monthly) == pytest.approx(120_000 / cost.total_monthly)
    r = assess(cost, 1_000_000, p, a, CLOSE)
    assert r.cash_available_at_purchase == pytest.approx(280_000)


# ------------------------------------------------------------------- flags


def test_flags_use_larger_of_the_two_monthly_figures():
    a = make_assumptions()
    # Combined ins/maint far above the itemized lines: plan basis is the larger.
    p = make_profile(carrying_costs__insurance_plus_maintenance_annual=40_000)
    cost = plan_cost(900_000, p, a)
    basis = plan_basis_monthly(cost, 900_000, p)
    assert basis > cost.total_monthly
    assert cost.total_monthly < 9_500 < basis
    r = assess(cost, 900_000, p, a, CLOSE)
    assert not r.within_comfort
    assert not r.over_hard_ceiling
    assert any("carry target" in b for b in r.blockers)


def test_comfortable_listing_has_no_blockers():
    a = make_assumptions()
    p = make_profile()
    r = assess(plan_cost(900_000, p, a), 900_000, p, a, CLOSE)
    assert r.within_comfort and r.blockers == [] and not r.over_hard_ceiling


def test_hard_ceiling_disqualifies():
    a = make_assumptions()
    p = make_profile()
    r = assess(plan_cost(1_300_000, p, a), 1_300_000, p, a, CLOSE)
    assert r.over_hard_ceiling and not r.within_comfort
    assert any("hard ceiling" in b for b in r.blockers)


def test_dti_back_end_formula():
    a = make_assumptions()
    p = make_profile()
    cost = plan_cost(1_000_000, p, a)
    r = assess(cost, 1_000_000, p, a, CLOSE)
    expected = (cost.total_monthly - cost.maintenance + 1_000) / 40_000
    assert r.dti_back_end == pytest.approx(expected)


# --------------------------------------------------------------- max_price


def _passes(price, sid, p, a):
    ea = effective_assumptions(p, a)
    s = next(s for s in scenarios_for(p, a) if s.id == sid)
    cost = loan_cost(make_listing(price), "memorial", X, s, ea)
    return assess(cost, price, p, a, CLOSE).within_comfort


def test_max_price_is_the_highest_passing_price():
    a = make_assumptions(mortgage__rate_30yr_fixed=0.0718)
    p = make_profile()
    result = max_price(p, "memorial", a, CLOSE)
    assert list(result) == ["plan", "plan_market", "conventional_20", "conventional_10"]
    assert result["plan_market"] < result["plan"]
    for sid, price in result.items():
        assert price % 1_000 == 0 and price > 0
        assert _passes(price, sid, p, a)
        assert not _passes(price + 1_000, sid, p, a)


def test_max_price_monotone_in_savings():
    a = make_assumptions()
    loose = {
        "carrying_costs__total_housing_carry_target_monthly": [],
        "monthly_budget_after_purchase__hard_ceiling_total_housing": None,
    }
    prev = None
    for balance in (150_000, 250_000, 350_000, 450_000):
        mp = max_price(make_profile(cash_for_purchase__balance=balance, **loose),
                       "memorial", a, CLOSE)
        if prev is not None:
            assert all(mp[k] > prev[k] for k in mp)     # cash-bound: strictly up
        prev = mp
    # And never decreases when cost, not cash, is binding.
    base = max_price(make_profile(), "memorial", a, CLOSE)
    rich = max_price(make_profile(cash_for_purchase__balance=3_000_000), "memorial", a, CLOSE)
    assert all(rich[k] >= base[k] for k in base)


def test_max_price_monotone_in_income():
    a = make_assumptions()
    prev = None
    for income in (10_000, 25_000, 40_000, 80_000):
        mp = max_price(make_profile(income__gross_monthly_for_lender=income),
                       "memorial", a, CLOSE)
        if prev is not None:
            assert all(mp[k] >= prev[k] for k in mp)
        prev = mp


def test_bigger_buffer_lowers_max_price():
    a = make_assumptions()
    loose = {
        "carrying_costs__total_housing_carry_target_monthly": [],
        "monthly_budget_after_purchase__hard_ceiling_total_housing": None,
    }
    small = max_price(make_profile(**loose), "memorial", a, CLOSE)
    big = max_price(make_profile(cash_for_purchase__keep_after_closing=100_000, **loose),
                    "memorial", a, CLOSE)
    assert all(big[k] < small[k] for k in small)
