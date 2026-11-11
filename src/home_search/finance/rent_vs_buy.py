"""Rent-vs-buy verdict per listing and for the search as a whole. WP8.

Method (full spec in docs/FINANCE_MODEL.md §3): simulate two households with the
same income and starting cash over the horizon. The buyer pays the down payment
and buying costs at month 0 and ownership costs each month, and ends with home
equity net of selling costs. The renter pays rent on a comparable home and
invests the buyer's month-0 outlay. Each month, whichever path is cheaper
invests the difference. Compare terminal net worth, discounted to today.

Conventions (all rates from `Assumptions`, none hard-coded):
- Months run 1..12*N; month m falls in year y = (m - 1) // 12 (year 0 first).
- Insurance and maintenance grow with home value, stepped once a year at
  `home_appreciation`; property tax grows at the lower of appreciation and
  `homestead_cap_annual`; P&I, flood insurance, PMI and HOA stay flat.
- Rent steps once a year at `rent_growth`; renters insurance stays flat.
- Investments compound monthly at the monthly equivalent of `return_nominal`.
  Gains are not taxed (neither side's portfolio is).
- Tax effect (with a profile and a marginal rate only): each year the buyer
  saves `rate x max(0, itemized - standard deduction)`, spread over its months.
  Itemized = interest on debt up to the cap + property tax up to the SALT cap.
  The standard deduction is indexed at `inflation`. Year y is tax year
  `first_tax_year + y`. Before `salt_revert_tax_year` the SALT cap is
  `max(salt_floor, salt_cap - salt_phaseout_rate x max(0, income -
  salt_phaseout_start))`, with income = `gross_monthly_for_lender x 12`; from
  that year on it is `salt_cap_after_revert`. Cap, threshold and income are
  held flat in nominal terms (their 1%/yr indexing is not modeled).

Reads the private profile: outputs go only into the emailed digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from ..config import Assumptions, Profile
from ..models import Listing, ListingKind, ListingStatus, LoanCost, RentVsBuy
from .affordability import effective_assumptions
from .costs import MONTHS_PER_YEAR, monthly_payment

FALLBACK_SOURCE = "fallback_ratio"

# Comparable-rental window (docs/FINANCE_MODEL.md §3 / rent_equivalent docstring).
COMP_SQFT_TOLERANCE = 0.20
COMP_BEDS_TOLERANCE = 1

VERDICT_BUY = "buy"
VERDICT_RENT = "rent"
VERDICT_CLOSE = "close_call"

# Sensitivity-grid dimensions, as they appear in the keys.
DIM_HORIZON, DIM_APPRECIATION, DIM_RATE = "h", "app", "rate"
DIM_LABELS = {
    DIM_HORIZON: "holding period",
    DIM_APPRECIATION: "home appreciation",
    DIM_RATE: "mortgage rate",
}


# ---------------------------------------------------------------- rent level


def _within(own: float | None, other: float | None, tolerance: float) -> bool:
    """True when the listing does not state the field, else when the rental's
    value is within `tolerance` of it (a rental missing the field fails)."""
    if own is None:
        return True
    return other is not None and abs(other - own) <= tolerance


def _is_comparable(listing: Listing, rental: Listing) -> bool:
    sqft_tol = COMP_SQFT_TOLERANCE * listing.sqft if listing.sqft is not None else 0.0
    return _within(listing.sqft, rental.sqft, sqft_tol) and _within(
        listing.beds, rental.beds, COMP_BEDS_TOLERANCE
    )


def rent_equivalent(listing: Listing, rentals: list[Listing], a: Assumptions) -> tuple[float, str]:
    """Monthly rent for a comparable home, and where it came from.

    Prefer the median of active rentals in the same high-school zone within
    +/-20% sqft and +/-1 bed; fall back to price / fallback_price_to_annual_rent.

    `rentals` must already be limited to the listing's high-school zone (the
    pipeline filters them; `Listing` carries no zoning). Only active RENT
    listings count. A size test applies when the listing states that field; a
    rental missing a field the listing has fails that test. A listing with
    neither sqft nor beds has no basis for comparison and takes the fallback.
    Source is `comp:<n> rentals` or `fallback_ratio`.
    """
    if listing.sqft is not None or listing.beds is not None:
        comps = [
            r.price for r in rentals
            if r.kind == ListingKind.RENT and r.status == ListingStatus.ACTIVE
            and r.price > 0 and _is_comparable(listing, r)
        ]
        if comps:
            return float(median(comps)), f"comp:{len(comps)} rentals"
    annual = listing.price / a.renting.fallback_price_to_annual_rent
    return annual / MONTHS_PER_YEAR, FALLBACK_SOURCE


# ---------------------------------------------------------------- simulation


@dataclass
class _Year1:
    """Year-1 monthly averages, for the rationale."""

    interest: float = 0.0
    tax: float = 0.0
    insurance: float = 0.0          # homeowners + flood
    maintenance: float = 0.0
    other: float = 0.0              # PMI + HOA
    rent: float = 0.0               # rent + renters insurance
    tax_benefit: float = 0.0        # annual


@dataclass
class _Path:
    """Buyer-minus-renter net worth at the end of each year, undiscounted."""

    diff_by_year: dict[int, float] = field(default_factory=dict)
    year1: _Year1 = field(default_factory=_Year1)


@dataclass(frozen=True)
class _TaxProfile:
    marginal_rate: float
    income: float                   # annual income proxy for the SALT phase-down


def _tax_profile(profile: Profile | None) -> _TaxProfile | None:
    if profile is None or profile.household.marginal_federal_rate is None:
        return None
    return _TaxProfile(
        marginal_rate=profile.household.marginal_federal_rate,
        income=profile.income.gross_monthly_for_lender * MONTHS_PER_YEAR,
    )


def salt_cap_for(tax_year: int, income: float, a: Assumptions) -> float:
    """SALT deduction cap for a tax year and income: the phased-down cap
    (never below the floor) before the revert year, the reverted cap from it on."""
    t = a.tax
    if tax_year >= t.salt_revert_tax_year:
        return float(t.salt_cap_after_revert)
    reduction = t.salt_phaseout_rate * max(0.0, income - t.salt_phaseout_start)
    return float(max(t.salt_floor, t.salt_cap - reduction))


def _tax_benefit(
    interest: float, prop_tax: float, loan_amount: float, year: int,
    household: _TaxProfile | None, a: Assumptions,
) -> float:
    if household is None:
        return 0.0
    t = a.tax
    deductible_share = min(1.0, t.mortgage_interest_debt_cap / loan_amount) if loan_amount > 0 else 0
    salt_cap = salt_cap_for(t.first_tax_year + year, household.income, a)
    itemized = interest * deductible_share + min(prop_tax, salt_cap)
    standard = t.standard_deduction_married_joint * (1 + a.investing.inflation) ** year
    return household.marginal_rate * max(0.0, itemized - standard)


def _simulate(
    price: float, cost: LoanCost, rent0: float, years: int, ea: Assumptions,
    household: _TaxProfile | None, *, appreciation: float, rate_delta: float = 0.0,
) -> _Path:
    own, ren, inv = ea.ownership, ea.renting, ea.investing
    rate = cost.rate + rate_delta
    payment = (
        cost.principal_interest if rate_delta == 0
        else round(monthly_payment(cost.loan_amount, rate, ea.mortgage.term_years), 2)
    )
    r_loan = rate / MONTHS_PER_YEAR
    r_inv = (1 + inv.return_nominal) ** (1 / MONTHS_PER_YEAR) - 1
    tax_growth = min(appreciation, ea.property_tax.homestead_cap_annual)
    flat_own = cost.flood_insurance + cost.pmi + cost.hoa
    renters_ins = ren.renters_insurance_annual / MONTHS_PER_YEAR

    balance = cost.loan_amount
    buyer_pf = 0.0
    renter_pf = cost.down_payment + price * own.closing_cost_buy_pct
    out = _Path()

    for year in range(years):
        value_factor = (1 + appreciation) ** year
        tax_m = cost.property_tax * (1 + tax_growth) ** year
        ins_m = cost.homeowners_insurance * value_factor
        maint_m = cost.maintenance * value_factor
        rent_m = rent0 * (1 + ren.rent_growth) ** year + renters_ins

        # Amortize the year first: the tax benefit depends on its interest.
        paid, interest_y = [], 0.0
        for _ in range(MONTHS_PER_YEAR):
            interest = balance * r_loan
            principal = min(max(0.0, payment - interest), balance)
            balance -= principal
            interest_y += interest
            paid.append(interest + principal)
        benefit = _tax_benefit(
            interest_y, tax_m * MONTHS_PER_YEAR, cost.loan_amount, year, household, ea
        )

        for pi in paid:
            buyer_cost = pi + tax_m + ins_m + maint_m + flat_own - benefit / MONTHS_PER_YEAR
            gap = buyer_cost - rent_m
            buyer_pf *= 1 + r_inv
            renter_pf *= 1 + r_inv
            if gap > 0:
                renter_pf += gap
            else:
                buyer_pf -= gap

        if year == 0:
            out.year1 = _Year1(
                interest=interest_y / MONTHS_PER_YEAR, tax=tax_m,
                insurance=cost.homeowners_insurance + cost.flood_insurance,
                maintenance=maint_m, other=cost.pmi + cost.hoa, rent=rent_m,
                tax_benefit=benefit,
            )
        value_end = price * (1 + appreciation) ** (year + 1)
        equity = value_end * (1 - own.closing_cost_sell_pct) - balance
        out.diff_by_year[year + 1] = equity + buyer_pf - renter_pf
    return out


def _discount(diff: float, years: int, a: Assumptions) -> float:
    return diff / (1 + a.investing.inflation) ** years


def _verdict(npv: float, price: float, a: Assumptions) -> str:
    band = a.rent_vs_buy_close_call_pct * price
    if npv > band:
        return VERDICT_BUY
    if npv < -band:
        return VERDICT_RENT
    return VERDICT_CLOSE


def sensitivity_key(horizon: int, appreciation: float, rate_delta: float) -> str:
    """`h=10,app=0.02,rate=+0.01`; a zero delta prints as `+0`."""
    return f"{DIM_HORIZON}={horizon},{DIM_APPRECIATION}={appreciation:g},{DIM_RATE}={rate_delta:+g}"


def _parse_key(key: str) -> dict[str, float]:
    return {k: float(v) for k, v in (part.split("=") for part in key.split(","))}


# ---------------------------------------------------------------- analysis


def _money(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _rationale(
    price: float, npv: float, horizon: int, rent0: float, source: str,
    y1: _Year1, breakeven: int | None, max_h: int, tax_modeled: bool,
    has_profile: bool,
) -> list[str]:
    ahead = "better" if npv >= 0 else "worse"
    if source == FALLBACK_SOURCE:
        where = "a fallback price-to-rent ratio"
    else:
        n = int(source.removeprefix("comp:").split()[0])
        where = f"the median of {n} comparable rental{'s' if n != 1 else ''}"
    out = [(
        f"Buying at {_money(price)} leaves you {_money(abs(npv))} {ahead} off than renting after "
        f"{horizon} years, in today's dollars, against a comparable rent of "
        f"{_money(rent0)}/month from {where}."
    )]
    unrecoverable = y1.interest + y1.tax + y1.insurance + y1.maintenance + y1.other
    extra = f" + PMI/HOA {_money(y1.other)}" if y1.other else ""
    out.append(
        f"In year 1 the unrecoverable cost of owning is {_money(unrecoverable)}/month "
        f"(interest {_money(y1.interest)} + tax {_money(y1.tax)} + insurance "
        f"{_money(y1.insurance)} + maintenance {_money(y1.maintenance)}{extra}) against "
        f"{_money(y1.rent)}/month of rent and renters insurance."
    )
    if breakeven is not None:
        out.append(f"Buying comes out ahead if you sell at the end of year {breakeven} or later.")
    else:
        out.append(f"Renting stays ahead at every holding period up to {max_h} years.")
    if not has_profile:
        out.append("The mortgage tax deduction is not modeled because no profile is loaded.")
    elif not tax_modeled:
        out.append(
            "The mortgage tax deduction is not modeled because the profile sets no "
            "marginal federal rate."
        )
    elif y1.tax_benefit > 0:
        out.append(
            f"Itemizing interest and property tax saves about {_money(y1.tax_benefit)} "
            "in federal tax in year 1."
        )
    else:
        out.append("Itemizing does not beat the standard deduction, so there is no tax benefit.")
    return out


def analyze(
    listing: Listing,
    cost: LoanCost,
    rentals: list[Listing],
    profile: Profile | None,
    a: Assumptions,
) -> RentVsBuy:
    """Rent vs buy for one listing under one loan cost (the pipeline passes
    `plan_market`, else `plan`). Profile overrides apply via `effective_assumptions`."""
    ea = effective_assumptions(profile, a)
    price = float(listing.price)
    rent0, source = rent_equivalent(listing, rentals, ea)
    household = _tax_profile(profile)

    sens = ea.sensitivity
    horizon = ea.horizon_years
    max_h = max(sens.horizons, default=horizon)
    years = max(horizon, max_h)
    base_app = ea.ownership.home_appreciation

    base = _simulate(price, cost, rent0, years, ea, household, appreciation=base_app)
    npv = _discount(base.diff_by_year[horizon], horizon, ea)
    breakeven = next(
        (y for y in range(1, max_h + 1) if base.diff_by_year[y] > 0), None
    )

    grid: dict[str, float] = {}
    sens_years = max(sens.horizons, default=0)
    if sens_years:
        for app in sens.appreciation:
            for dr in sens.rate:
                path = (
                    base if app == base_app and dr == 0 else
                    _simulate(price, cost, rent0, sens_years, ea, household,
                              appreciation=app, rate_delta=dr)
                )
                for h in sens.horizons:
                    grid[sensitivity_key(h, app, dr)] = round(
                        _discount(path.diff_by_year[h], h, ea), 2
                    )

    return RentVsBuy(
        horizon_years=horizon,
        rent_equivalent_monthly=round(rent0, 2),
        rent_source=source,
        npv_buy_minus_rent=round(npv, 2),
        breakeven_year=breakeven,
        verdict=_verdict(npv, price, ea),
        rationale=_rationale(
            price, npv, horizon, rent0, source, base.year1, breakeven, max_h,
            household is not None, profile is not None,
        ),
        sensitivity=grid,
    )


# ---------------------------------------------------------------- portfolio


def _swing_factor(results: list[RentVsBuy]) -> dict | None:
    """The grid dimension whose range most often flips the sign of the NPV
    (buy ahead vs rent ahead), ties broken by the mean NPV range it spans.

    For each result and each fixed combination of the other two dimensions,
    vary one dimension across its grid values and record whether the NPV
    changes sign and how far it moves.
    """
    stats: dict[str, dict[str, float]] = {}
    for dim in DIM_LABELS:
        flips = lines = 0
        spread = 0.0
        for r in results:
            groups: dict[tuple, list[float]] = {}
            for key, npv in r.sensitivity.items():
                parts = _parse_key(key)
                rest = tuple(sorted((k, v) for k, v in parts.items() if k != dim))
                groups.setdefault(rest, []).append(npv)
            for values in groups.values():
                if len(values) < 2:
                    continue
                lines += 1
                spread += max(values) - min(values)
                if min(values) < 0 < max(values):
                    flips += 1
        if lines:
            stats[dim] = {"flip_share": flips / lines, "mean_npv_range": spread / lines}
    if not stats:
        return None
    dim = max(stats, key=lambda d: (stats[d]["flip_share"], stats[d]["mean_npv_range"]))
    return {
        "dimension": dim,
        "label": DIM_LABELS[dim],
        "flip_share": round(stats[dim]["flip_share"], 4),
        "mean_npv_range": round(stats[dim]["mean_npv_range"], 2),
    }


def portfolio_view(results: list[RentVsBuy]) -> dict:
    """The digest's headline: share of candidates where buying wins, median
    break-even year, and the single assumption the verdict is most sensitive to.

    Keys: count, verdicts (counts per verdict), buy_share, median_npv,
    median_breakeven_year (over results that break even), never_break_even
    (count), swing_factor (None, or dimension/label/flip_share/mean_npv_range).
    """
    n = len(results)
    verdicts = {v: sum(r.verdict == v for r in results)
                for v in (VERDICT_BUY, VERDICT_CLOSE, VERDICT_RENT)}
    breakevens = [r.breakeven_year for r in results if r.breakeven_year is not None]
    return {
        "count": n,
        "verdicts": verdicts,
        "buy_share": verdicts[VERDICT_BUY] / n if n else None,
        "median_npv": round(median(r.npv_buy_minus_rent for r in results), 2) if n else None,
        "median_breakeven_year": median(breakevens) if breakevens else None,
        "never_break_even": n - len(breakevens),
        "swing_factor": _swing_factor(results),
    }
