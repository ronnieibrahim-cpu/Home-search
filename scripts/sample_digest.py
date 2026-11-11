"""Render a SAMPLE weekly digest from synthetic data, for previewing the layout.

    python3 scripts/sample_digest.py

Writes out/sample-digest.html and out/sample-digest.txt (out/ is gitignored).
Every listing, address, URL and household number below is made up; the real
search.yaml and assumptions.yaml (both public) drive the costs, scores and
rent-vs-buy figures through the same code the pipeline uses. Nothing is sent.

`build_sample()` is also used by tests/test_digest.py.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from home_search import config, scoring
from home_search.digest import render
from home_search.finance import affordability, rent_vs_buy
from home_search.models import (
    Candidate,
    ChangeEvent,
    FloodRisk,
    Listing,
    ListingKind,
    MarketSnapshot,
    SchoolZoning,
    ZoningConfidence,
)

OUT = ROOT / "out"
BANNER = ("SAMPLE: synthetic listings and a made-up household, for layout review only. "
          "Nothing here is a real listing or a real number.")
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
WEEK = date(2026, 11, 9)
PREV_WEEK = WEEK - timedelta(days=7)

# A made-up household. Not the owner's profile.
SAMPLE_PROFILE = {
    "meta": {"generated": "2026-11-01", "target_close_date": "2027-06-01"},
    "search": {"planning_price": 1_050_000},
    "income": {"gross_monthly_for_lender": 41_000},
    "cash_for_purchase": {"balance": 290_000, "monthly_addition": 6_500,
                          "keep_after_closing": 60_000},
    "other_assets": {"retirement": 180_000},
    "financing": {"down_payment_pct": 0.20, "rate_assumption": 0.0625,
                  "rate_range": [0.0575, 0.07]},
    "carrying_costs": {"property_tax_rate_modeled": 0.022,
                       "insurance_plus_maintenance_annual": 16_000,
                       "total_housing_carry_target_monthly": [8_000, 9_500]},
    "monthly_budget_after_purchase": {"hard_ceiling_total_housing": 10_500},
    "household": {"marginal_federal_rate": 0.24, "monthly_non_housing_debt": 800},
}

BELLAIRE_HS = SchoolZoning(
    district="HISD", elementary="Condit Elementary", middle="Pershing Middle School",
    high="Bellaire High School", confidence=ZoningConfidence.VERIFIED,
    boundary_vintage="2025-26",
)
BELLAIRE_HS_2 = BELLAIRE_HS.model_copy(update={"elementary": "Horn Elementary"})
MEMORIAL_HS = SchoolZoning(
    district="SBISD", elementary="Bunker Hill Elementary", middle="Memorial Middle School",
    high="Memorial High School", confidence=ZoningConfidence.VERIFIED,
    boundary_vintage="2025-26",
)
CLAIM = SchoolZoning(high="Memorial High School", elementary="Frostwood Elementary",
                     confidence=ZoningConfidence.LISTING_CLAIM)
UNKNOWN = SchoolZoning()


def _listing(n: int, address: str, zip_code: str, price: int, *, kind=ListingKind.SALE,
             city="Bellaire", **over) -> Listing:
    fields = {
        "source": "sample", "source_id": f"sample-{n}",
        "url": f"https://example.com/sample-listing/{n}", "kind": kind,
        "address": address, "city": city, "zip": zip_code, "price": price,
        "beds": 4, "baths": 3.5, "sqft": 3200, "lot_sqft": 7500, "year_built": 2008,
        "days_on_market": 12, "fetched_at": NOW,
    }
    fields.update(over)
    return Listing(**fields)


# --- synthetic listings: 3 verified candidates across both areas, 2 unverified, 2 rentals
SALES = [
    (_listing(1, "4501 Sample Ave", "77401", 1_049_000, sqft=3350, days_on_market=21,
              description="Updated kitchen, never flooded per seller, large backyard."),
     BELLAIRE_HS, "bellaire", FloodRisk(fema_zone="X"), ["price_cut"]),
    (_listing(2, "4620 Example St", "77401", 1_189_000, beds=5, baths=4.5, sqft=3900,
              year_built=2015, days_on_market=5),
     BELLAIRE_HS_2, "bellaire", FloodRisk(fema_zone="X_shaded"), ["new"]),
    (_listing(3, "812 Placeholder Ln", "77024", 1_125_000, city="Houston", sqft=3100,
              lot_sqft=11_000, year_built=1968, days_on_market=48,
              description="Original ranch on an oversized lot. Elevated slab."),
     MEMORIAL_HS, "memorial", FloodRisk(fema_zone="X", notes=[
         "Description says the house is elevated; ask for the elevation certificate."]),
     []),
]
UNVERIFIED = [
    (_listing(4, "1350 Demo Ct", "77079", 985_000, city="Houston", sqft=2900,
              description="Zoned to Frostwood / Memorial HS per listing."),
     CLAIM, "memorial", FloodRisk(fema_zone="X"), ["new"]),
    (_listing(5, "6011 Mock Blvd", "77096", 899_000, city="Houston", sqft=2750, lat=None,
              lon=None),
     UNKNOWN, "bellaire", FloodRisk(notes=[
         "No coordinates for this listing; FEMA flood zone not checked."]), []),
]
RENTALS = [
    (_listing(6, "4419 Fictional Dr", "77401", 5_400, kind=ListingKind.RENT, sqft=3100),
     BELLAIRE_HS, "bellaire", FloodRisk(fema_zone="X")),
    (_listing(7, "905 Stand-in Way", "77024", 6_200, kind=ListingKind.RENT, city="Houston",
              sqft=3300),
     MEMORIAL_HS, "memorial", FloodRisk(fema_zone="X")),
]

MARKET = [
    MarketSnapshot(area_id="bellaire", week_of=PREV_WEEK, active_count=14,
                   median_price=1_095_000, median_ppsf=338.5, median_dom=27,
                   median_rent=5_300, price_cut_share=0.21),
    MarketSnapshot(area_id="bellaire", week_of=WEEK, active_count=16,
                   median_price=1_080_000, median_ppsf=335.0, median_dom=25,
                   median_rent=5_400, price_cut_share=0.25),
    MarketSnapshot(area_id="memorial", week_of=PREV_WEEK, active_count=9,
                   median_price=1_150_000, median_ppsf=362.0, median_dom=33,
                   median_rent=6_100, price_cut_share=0.11),
    MarketSnapshot(area_id="memorial", week_of=WEEK, active_count=8,
                   median_price=1_160_000, median_ppsf=365.2, median_dom=36,
                   median_rent=6_200, price_cut_share=0.13),
]


def _evaluate(listing, zoning, area_id, risk, flags, cfg, a, profile, comps, market):
    c = Candidate(listing=listing, zoning=zoning, flood=risk, flags=list(flags))
    c.costs = affordability.costs_for(listing, area_id, risk, profile, a)
    cost = scoring.primary_cost(c.costs)
    when = affordability.target_close_date(profile, cfg.purchase.target_year)
    c.affordability = affordability.assess(cost, listing.price, profile, a, when)
    c.rent_vs_buy = rent_vs_buy.analyze(listing, cost, comps, profile, a)
    c.score_parts = scoring.score_parts(c, cfg, market, profile)
    c.score = scoring.weighted(c.score_parts, cfg)
    c.flags = scoring.flags(c, cfg)
    return c


def build_sample() -> dict:
    """Keyword arguments for `render.weekly`, all synthetic."""
    cfg = config.load_search()
    a = config.load_assumptions()
    profile = config.Profile.model_validate(SAMPLE_PROFILE)
    latest = {s.area_id: s for s in MARKET if s.week_of == WEEK}
    rentals = [Candidate(listing=r, zoning=z, flood=f) for r, z, _, f in RENTALS]
    for rc in rentals:
        rc.flags = scoring.flags(rc, cfg)

    def comps(area_id):
        return [r for r, _, ar, _ in RENTALS if ar == area_id]

    def build(rows):
        return [_evaluate(lst, z, ar, f, fl, cfg, a, profile, comps(ar), latest[ar])
                for lst, z, ar, f, fl in rows]

    order = (lambda c: (-c.score, c.listing.price))
    candidates = sorted(build(SALES), key=order)
    unverified = sorted(build(UNVERIFIED), key=order)

    when = affordability.target_close_date(profile, cfg.purchase.target_year)
    pool = candidates + unverified
    priced = sorted(candidates, key=lambda c: c.listing.price)
    mid = priced[(len(priced) - 1) // 2]
    mid_cost = scoring.primary_cost(mid.costs)
    advisor = {
        "banner": BANNER,
        "has_profile": True,
        "portfolio": rent_vs_buy.portfolio_view([c.rent_vs_buy for c in pool]),
        "price_band": {"min": cfg.purchase.price_min, "max": cfg.purchase.price_max,
                       "stretch_max": cfg.purchase.stretch_max},
        "max_price": {area.id: affordability.max_price(profile, area.id, a, when)
                      for area in cfg.areas},
        "close_date": when,
        "cash_at_close": round(affordability.cash_at(
            profile, when, affordability.effective_assumptions(profile, a)), 2),
        "median_candidate_key": mid.listing.key,
        "median_candidate_price": mid.listing.price,
        "median_candidate_cash_needed": mid.affordability.cash_needed,
        "median_candidate_reserve_months": affordability.reserve_months(
            profile, mid_cost.total_monthly),
        "planning_price": profile.search.planning_price,
        "plan_rate_bracket": affordability.plan_rate_bracket(
            profile.search.planning_price, profile, a),
        "unverified": unverified,
        "unverified_total": len(unverified),
        "candidates_total": len(candidates),
        "dropped": {"floodway": 1, "non_target_high": 3, "excluded_keyword": 1},
        "stage_failures": {},
        "missing_boundaries": [],
        "assumptions_as_of": a.as_of,
    }
    k = {c.listing.address: c.listing.key for c in pool + rentals}
    at = datetime(2026, 11, 8, 9, tzinfo=UTC)
    changes = [
        ChangeEvent(key=k["4620 Example St"], kind="new", new="1189000", at=at),
        ChangeEvent(key=k["1350 Demo Ct"], kind="new", new="985000", at=at),
        ChangeEvent(key=k["4501 Sample Ave"], kind="price_change", old="1099000",
                    new="1049000", at=at - timedelta(days=2)),
        ChangeEvent(key="sale|77401|4777 elsewhere st", kind="status_change", old="active",
                    new="pending", at=at),
    ]
    outages = ["redfin_csv: HTTP 403 (blocked); no retry this run"]
    return {"candidates": candidates, "rentals": rentals, "changes": changes,
            "market": MARKET, "advisor": advisor, "outages": outages}


def render_sample() -> render.Email:
    email = render.weekly(**build_sample())
    return render.Email(subject=f"[SAMPLE] {email.subject}", html=email.html, text=email.text)


def write_sample(out_dir: Path = OUT) -> tuple[Path, Path]:
    email = render_sample()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path, text_path = out_dir / "sample-digest.html", out_dir / "sample-digest.txt"
    html_path.write_text(email.html, encoding="utf-8")
    text_path.write_text(f"Subject: {email.subject}\n\n{email.text}", encoding="utf-8")
    return html_path, text_path


def main() -> int:
    html_path, text_path = write_sample()
    print(f"wrote {html_path.relative_to(ROOT)} and {text_path.relative_to(ROOT)} (synthetic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
