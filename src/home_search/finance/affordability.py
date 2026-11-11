"""Can this household carry this listing, and what blocks it if not. WP7.

Reads the private profile. Outputs go only into the emailed digest.
Spec: docs/FINANCE_MODEL.md §2. Never log or print profile values.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from ..config import Assumptions, LoanScenario, Profile
from ..models import Affordability, FloodRisk, Listing, ListingKind, LoanCost
from .costs import MONTHS_PER_YEAR, all_scenarios, default_scenarios, loan_cost, monthly_payment

PLAN_SCENARIO_ID = "plan"
PLAN_MARKET_SCENARIO_ID = "plan_market"

# Default close date when the profile has none: June 1 of the purchase year
# (config.ProfileMeta.target_close_date).
DEFAULT_CLOSE_MONTH = 6
DEFAULT_CLOSE_DAY = 1

# max_price resolution: the answer is a multiple of this many dollars.
PRICE_STEP = 1_000
# Upper bound on the doubling search in max_price (STEP * 2**N dollars).
_MAX_DOUBLINGS = 40


# ------------------------------------------------------------ profile overlay


def effective_assumptions(profile: Profile | None, a: Assumptions) -> Assumptions:
    """`a` with the profile's values applied wherever both exist.

    Overrides: financing.closing_costs_pct -> ownership.closing_cost_buy_pct,
    carrying_costs.hoa_monthly -> ownership.hoa_monthly_default.
    The market rate (mortgage.rate_30yr_fixed) is never overridden: the
    household's planning rate is carried by the `plan` scenario's spread.
    The modeled tax rate is applied only to the plan-basis figure (§2), not to
    the itemized §1 cost, which keeps the per-area rate and exemption.
    Idempotent; `a` is not modified.
    """
    if profile is None:
        return a
    fin, carry = profile.financing, profile.carrying_costs
    own_update: dict = {}
    if fin.closing_costs_pct is not None:
        own_update["closing_cost_buy_pct"] = fin.closing_costs_pct
    if carry.hoa_monthly is not None:
        own_update["hoa_monthly_default"] = carry.hoa_monthly
    if not own_update:
        return a
    return a.model_copy(update={"ownership": a.ownership.model_copy(update=own_update)})


def plan_scenario(profile: Profile, a: Assumptions) -> LoanScenario:
    """The household's own scenario: its down payment, no PMI, at its planning rate.

    The planning rate (`financing.rate_assumption`) is carried as a spread over
    the market rate, so `plan` prices at exactly that rate (before any jumbo
    spread). Without a planning rate the spread is 0, i.e. the market rate.
    """
    rate = profile.financing.rate_assumption
    spread = rate - a.mortgage.rate_30yr_fixed if rate is not None else 0.0
    return LoanScenario(
        id=PLAN_SCENARIO_ID, down_pct=profile.financing.down_payment_pct, pmi=False,
        rate_spread=spread,
    )


def plan_market_scenario(profile: Profile, a: Assumptions) -> LoanScenario:
    """`plan`'s down payment at today's market rate (spread 0), no PMI."""
    return LoanScenario(
        id=PLAN_MARKET_SCENARIO_ID, down_pct=profile.financing.down_payment_pct, pmi=False
    )


def scenarios_for(profile: Profile | None, a: Assumptions) -> list[LoanScenario]:
    """Scenarios to price, in digest order.

    With a profile: `plan` (planning rate), `plan_market` (market rate; only
    when the profile sets a planning rate, else it would equal `plan`), then the
    assumptions' scenarios at the market rate; the physician scenario only when
    `financing.physician_loan_programs` is set.
    Without a profile: the assumptions' scenarios except the physician one.
    """
    if profile is None:
        return default_scenarios(a)
    own = [plan_scenario(profile, a)]
    if profile.financing.rate_assumption is not None:
        own.append(plan_market_scenario(profile, a))
    rest = a.mortgage.scenarios if profile.financing.physician_loan_programs else (
        default_scenarios(a)
    )
    reserved = {s.id for s in own}
    return own + [s for s in rest if s.id not in reserved]


def costs_for(
    listing: Listing, area_id: str, flood: FloodRisk, profile: Profile | None, a: Assumptions
) -> list[LoanCost]:
    """All scenario costs for a listing, profile overrides applied. Plan first."""
    return all_scenarios(
        listing, area_id, flood, effective_assumptions(profile, a),
        scenarios=scenarios_for(profile, a),
    )


# ----------------------------------------------------------------------- cash


def target_close_date(profile: Profile, target_year: int) -> date:
    """`meta.target_close_date`, else June 1 of `search.purchase.target_year`."""
    if profile.meta.target_close_date is not None:
        return profile.meta.target_close_date
    return date(target_year, DEFAULT_CLOSE_MONTH, DEFAULT_CLOSE_DAY)


def whole_months_between(start: date, end: date) -> int:
    """Completed calendar months from `start` to `end`; 0 if `end` is not later.

    2026-11-11 -> 2027-06-01 is 6 (the 7th month completes on 2027-06-11).
    """
    months = (end.year - start.year) * MONTHS_PER_YEAR + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


def cash_at(profile: Profile, when: date, a: Assumptions) -> float:
    """cash_for_purchase.balance grown by monthly_addition from meta.generated to
    `when`, minus keep_after_closing. The buffer is never reduced to make a deal work."""
    c = profile.cash_for_purchase
    generated = profile.meta.generated
    months = whole_months_between(generated, when) if generated is not None else 0
    return c.balance + c.monthly_addition * months - c.keep_after_closing


def reserve_months(profile: Profile, total_monthly: float) -> float | None:
    """`other_assets` as months of housing cost. Reported only, never added to cash."""
    if total_monthly <= 0 or not profile.other_assets:
        return None
    return sum(profile.other_assets.values()) / total_monthly


# --------------------------------------------------------------------- assess


def plan_basis_tax_rate(profile: Profile, listing_tax_rate: float | None = None) -> float | None:
    """Tax rate for the plan-basis figure: the listing's actual rate when the
    source provides one (no source does yet, so callers pass None), else the
    profile's modeled rate. None means fall back to the itemized tax."""
    if listing_tax_rate is not None:
        return listing_tax_rate
    return profile.carrying_costs.property_tax_rate_modeled


def plan_basis_monthly(cost: LoanCost, price: int, profile: Profile) -> float:
    """P&I + P x tax_rate / 12 + insurance_plus_maintenance_annual / 12 (the
    household workbook's convention). Missing profile figures fall back to the
    matching itemized lines of `cost`."""
    rate = plan_basis_tax_rate(profile)
    tax = round(price * rate / MONTHS_PER_YEAR, 2) if rate is not None else cost.property_tax
    combined = profile.carrying_costs.insurance_plus_maintenance_annual
    ins_maint = (
        round(combined / MONTHS_PER_YEAR, 2)
        if combined is not None
        else cost.homeowners_insurance + cost.maintenance
    )
    return round(cost.principal_interest + tax + ins_maint, 2)


def _money(x: float) -> str:
    return f"${x:,.0f}"


def assess(cost: LoanCost, price: int, profile: Profile, a: Assumptions, when: date) -> Affordability:
    ea = effective_assumptions(profile, a)
    usable = cash_at(profile, when, ea)
    down_pct = cost.down_payment / price if price > 0 else 0.0
    closing_pct = ea.ownership.closing_cost_buy_pct
    cash_needed = price * (down_pct + closing_pct)

    total = cost.total_monthly
    basis = plan_basis_monthly(cost, price, profile)
    carry = max(total, basis)

    blockers: list[str] = []

    flag_above = profile.carry_flag_above
    over_carry = flag_above is not None and carry > flag_above
    if over_carry:
        blockers.append(
            f"Monthly housing cost {_money(carry)} is above the carry target of "
            f"{_money(flag_above)}."
        )

    ceiling = profile.monthly_budget_after_purchase.hard_ceiling_total_housing
    over_ceiling = ceiling is not None and carry > ceiling
    if over_ceiling:
        blockers.append(
            f"Monthly housing cost {_money(carry)} is above the hard ceiling of "
            f"{_money(ceiling)}."
        )

    cash_short = cash_needed > usable
    if cash_short:
        share = down_pct + closing_pct
        closes_at = max(0.0, usable) / share if share > 0 else 0.0
        closes_at = int(closes_at // PRICE_STEP) * PRICE_STEP
        blockers.append(
            f"Cash short by {_money(cash_needed - usable)} at closing; "
            f"closes at a price of {_money(closes_at)} or less."
        )

    income = profile.income.gross_monthly_for_lender
    debt = total - cost.maintenance + profile.household.monthly_non_housing_debt
    dti = debt / income if income > 0 else float("inf")

    return Affordability(
        cash_needed=round(cash_needed, 2),
        cash_available_at_purchase=round(usable, 2),
        dti_back_end=dti,
        plan_basis_monthly=basis,
        within_comfort=not (over_carry or over_ceiling or cash_short),
        over_hard_ceiling=over_ceiling,
        blockers=blockers,
    )


def plan_rate_bracket(price: int, profile: Profile, a: Assumptions) -> dict[float, float]:
    """`plan` P&I at each end of `financing.rate_range`, for the digest."""
    m = a.mortgage
    loan = price * (1 - profile.financing.down_payment_pct)
    jumbo = m.jumbo_rate_spread if loan > m.conforming_limit else 0.0
    return {
        r: round(monthly_payment(loan, r + jumbo, m.term_years), 2)
        for r in profile.financing.rate_range
    }


# ------------------------------------------------------------------ max price


def _probe_listing(price: int, when: date) -> Listing:
    return Listing(
        source="max_price", source_id="probe", url="", kind=ListingKind.SALE,
        address="", city="", zip="", price=price,
        fetched_at=datetime.combine(when, time(tzinfo=UTC)),
    )


def _cheapest_flood(a: Assumptions) -> FloodRisk:
    priced = {z: p for z, p in a.insurance.flood_annual_by_zone.items() if p is not None}
    return FloodRisk(fema_zone=min(priced, key=priced.__getitem__) if priced else None)


def max_price(profile: Profile, area_id: str, a: Assumptions, when: date) -> dict[str, int]:
    """Highest price that passes `assess` under each loan scenario. Shown at the
    top of every digest so the budget band is tested against the real numbers."""
    ea = effective_assumptions(profile, a)
    flood = _cheapest_flood(ea)

    def passes(price: int, scenario: LoanScenario) -> bool:
        cost = loan_cost(_probe_listing(price, when), area_id, flood, scenario, ea)
        return assess(cost, price, profile, a, when).within_comfort

    out: dict[str, int] = {}
    for s in scenarios_for(profile, a):
        if not passes(PRICE_STEP, s):
            out[s.id] = 0
            continue
        lo, hi = 1, 2                     # in units of PRICE_STEP; lo passes
        for _ in range(_MAX_DOUBLINGS):
            if not passes(hi * PRICE_STEP, s):
                break
            lo, hi = hi, hi * 2
        else:
            out[s.id] = lo * PRICE_STEP   # nothing in the profile limits this scenario
            continue
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if passes(mid * PRICE_STEP, s):
                lo = mid
            else:
                hi = mid
        out[s.id] = lo * PRICE_STEP
    return out
