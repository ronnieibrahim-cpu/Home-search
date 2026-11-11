"""Monthly cost of owning a specific listing under each loan scenario. WP7.

Pure functions: no I/O, no profile. Spec and worked example in docs/FINANCE_MODEL.md.

Every rate and limit comes from `Assumptions`. Callers that have a profile pass
`affordability.effective_assumptions(profile, a)` so the household's overrides
(rate, modeled tax rate, HOA) apply, and pick scenarios with
`affordability.scenarios_for(profile, a)`.

Each monthly line item is rounded to the cent, so the itemized total adds up
the way it is printed in the digest (and in the spec's worked example).
"""

from __future__ import annotations

from ..config import Assumptions, LoanScenario
from ..models import FloodRisk, Listing, LoanCost

MONTHS_PER_YEAR = 12

# Scenario ids starting with this are priced only when the profile opts in
# (financing.physician_loan_programs); see `default_scenarios`.
PHYSICIAN_PREFIX = "physician"

# Zone used when FEMA returned nothing or a zone the table does not price
# (docs/FINANCE_MODEL.md §1: "unknown zone priced as X_shaded").
UNKNOWN_FLOOD_ZONE = "X_shaded"
FLOODWAY_ZONE = "floodway"


def _cents(x: float) -> float:
    return round(x, 2)


def monthly_payment(principal: float, annual_rate: float, years: int) -> float:
    """Standard fixed-rate amortization payment. Rate 0 must not divide by zero."""
    n = years * MONTHS_PER_YEAR
    if principal <= 0 or n <= 0:
        return 0.0
    r = annual_rate / MONTHS_PER_YEAR
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def property_tax_rate(listing: Listing, area_id: str, a: Assumptions) -> float:
    """Total tax rate applied to this listing.

    The single place a listing-specific rate (e.g. from the HCAD record or the
    source) would plug in once `Listing` carries one; today it is the area rate.
    """
    return a.property_tax.rate_by_area[area_id]


def flood_premium_annual(flood: FloodRisk, a: Assumptions) -> float:
    """Annual flood premium for the zone. Unknown zones are priced as X_shaded.

    A floodway has no premium in the table (it disqualifies the listing in
    scoring); it is priced at the highest premium the table lists so the cost
    is never understated.
    """
    table = a.insurance.flood_annual_by_zone
    zone = FLOODWAY_ZONE if flood.in_floodway else flood.fema_zone
    if zone not in table:
        zone = UNKNOWN_FLOOD_ZONE
    premium = table.get(zone)
    if premium is None:
        priced = [v for v in table.values() if v is not None]
        premium = max(priced) if priced else 0
    return float(premium)


def loan_cost(
    listing: Listing, area_id: str, flood: FloodRisk, scenario: LoanScenario, a: Assumptions
) -> LoanCost:
    m = a.mortgage
    price = float(listing.price)
    down = price * scenario.down_pct
    loan = price * (1 - scenario.down_pct)
    is_jumbo = loan > m.conforming_limit
    rate = m.rate_30yr_fixed + scenario.rate_spread + (m.jumbo_rate_spread if is_jumbo else 0.0)

    pt = a.property_tax
    exempt = pt.homestead_exemption_school * pt.school_share_by_area[area_id]
    taxable = max(0.0, price - exempt)
    tax = taxable * property_tax_rate(listing, area_id, a) / MONTHS_PER_YEAR

    hoa = listing.hoa_monthly if listing.hoa_monthly is not None else a.ownership.hoa_monthly_default

    return LoanCost(
        scenario_id=scenario.id,
        down_payment=_cents(down),
        loan_amount=_cents(loan),
        rate=rate,
        is_jumbo=is_jumbo,
        principal_interest=_cents(monthly_payment(loan, rate, m.term_years)),
        property_tax=_cents(tax),
        homeowners_insurance=_cents(
            price * a.insurance.homeowners_pct_of_value / MONTHS_PER_YEAR
        ),
        flood_insurance=_cents(flood_premium_annual(flood, a) / MONTHS_PER_YEAR),
        pmi=_cents(loan * m.pmi_annual_pct / MONTHS_PER_YEAR) if scenario.pmi else 0.0,
        hoa=_cents(float(hoa)),
        maintenance=_cents(price * a.ownership.maintenance_pct_of_value / MONTHS_PER_YEAR),
    )


def default_scenarios(a: Assumptions) -> list[LoanScenario]:
    """The assumptions' scenarios without the opt-in physician loan (no-profile case)."""
    return [s for s in a.mortgage.scenarios if not s.id.startswith(PHYSICIAN_PREFIX)]


def all_scenarios(
    listing: Listing,
    area_id: str,
    flood: FloodRisk,
    a: Assumptions,
    *,
    scenarios: list[LoanScenario] | None = None,
) -> list[LoanCost]:
    """Price the listing under each scenario, in order.

    `scenarios` defaults to `default_scenarios(a)`; with a profile pass
    `affordability.scenarios_for(profile, a)` (plan first).
    """
    chosen = default_scenarios(a) if scenarios is None else scenarios
    return [loan_cost(listing, area_id, flood, s, a) for s in chosen]
