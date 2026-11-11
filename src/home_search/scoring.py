"""0-100 candidate score from the weighted parts in search.yaml. WP9.

Each part returns 0-100 and is recorded in Candidate.score_parts so the digest
can show why a listing ranked where it did. Hard disqualifiers (floodway, flood
zone beyond the household's tolerance, high school verified to a non-target,
excluded keyword, price above stretch_max, over the hard ceiling) return None
from `score` and the listing is dropped from candidates.

Every breakpoint lives in search.yaml `scoring.params`; see docs/ARCHITECTURE.md
"Scoring" for the table this implements.

History-derived flags (`new`, `price_cut`, `back_on_market`) need the store, so
the pipeline puts them on `Candidate.flags` before calling `score`; the value
part reads `price_cut` from there and `flags` keeps them.
"""

from __future__ import annotations

from .config import Household, Profile, ScoringParams, SearchConfig
from .enrich.flood import NOTE_PRIOR_FLOODING
from .enrich.schools import area_for, is_target, same_school
from .models import Candidate, ListingKind, LoanCost, MarketSnapshot, ZoningConfidence

PART_NAMES = ("schools", "value", "flood", "fit", "carrying_cost")

PLAN_MARKET_ID = "plan_market"
PLAN_ID = "plan"

FLOODWAY = "floodway"
# Zones each `household.flood_tolerance` accepts. An unknown zone (None) is
# never disqualified here; it is flagged `flood_unknown` and scored low.
FLOOD_TOLERANCE_ZONES: dict[str, frozenset[str]] = {
    "x_only": frozenset({"X"}),
    "allow_500yr": frozenset({"X", "X_shaded"}),
    "allow_100yr": frozenset({"X", "X_shaded", "AE"}),
}

# Flags set by the pipeline from store history; `flags` keeps them.
HISTORY_FLAGS = ("new", "price_cut", "back_on_market")


# ----------------------------------------------------------------- helpers


def params(cfg: SearchConfig) -> ScoringParams:
    p = cfg.scoring.params
    if p is None:
        raise ValueError("search.yaml is missing scoring.params (see docs/ARCHITECTURE.md)")
    return p


def primary_cost(costs: list[LoanCost]) -> LoanCost | None:
    """The cost the pipeline reasons with: `plan_market`, else `plan`, else the first."""
    by_id = {c.scenario_id: c for c in costs}
    return by_id.get(PLAN_MARKET_ID) or by_id.get(PLAN_ID) or (costs[0] if costs else None)


def _linear(x: float, full_at: float, zero_at: float) -> float:
    """100 at or beyond `full_at`, 0 at or beyond `zero_at`, linear between."""
    if full_at == zero_at:
        return 100.0 if x <= full_at else 0.0
    t = (x - full_at) / (zero_at - full_at)
    return 100.0 * min(1.0, max(0.0, 1.0 - t))


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def _flood_tolerance(profile: Profile | None) -> str:
    return (profile.household if profile is not None else Household()).flood_tolerance


def _is_floodway(c: Candidate) -> bool:
    return c.flood.in_floodway or c.flood.fema_zone == FLOODWAY


# ----------------------------------------------------------- disqualifiers


def disqualifier(c: Candidate, cfg: SearchConfig, profile: Profile | None) -> str | None:
    """The first hard disqualifier that applies, or None. Reasons are short codes."""
    listing = c.listing
    if _is_floodway(c):
        return "floodway"
    zone = c.flood.fema_zone
    if zone is not None and zone not in FLOOD_TOLERANCE_ZONES[_flood_tolerance(profile)]:
        return "flood_tolerance"
    area = area_for(listing, c.zoning, cfg)
    if area is None:
        return "outside_areas"
    if c.zoning.confidence == ZoningConfidence.VERIFIED and not is_target(c.zoning, area, cfg):
        return "non_target_high"
    text = " ".join(filter(None, (listing.address, listing.description))).lower()
    if any(k.lower() in text for k in cfg.home.exclude_keywords):
        return "excluded_keyword"
    if listing.kind == ListingKind.SALE and listing.price > cfg.purchase.stretch_max:
        return "over_stretch_max"
    if c.affordability is not None and c.affordability.over_hard_ceiling:
        return "over_hard_ceiling"
    return None


# ------------------------------------------------------------------- parts


def schools_part(c: Candidate, cfg: SearchConfig) -> float:
    p = params(cfg)
    z = c.zoning
    area = area_for(c.listing, z, cfg)
    if area is None:
        return p.schools_unknown
    ts = area.target_schools
    high_target = any(same_school(z.high, t) for t in ts.high)
    if not high_target:
        return p.schools_unknown
    if z.confidence != ZoningConfidence.VERIFIED:
        return p.schools_claim
    elem = any(same_school(z.elementary, e) for e in ts.elementary)
    if ts.preferred_high is None:
        return p.schools_full if elem else p.schools_verified_high
    preferred = same_school(z.high, ts.preferred_high)
    if preferred and elem:
        return p.schools_full
    if preferred or elem:
        return p.schools_partial
    return p.schools_verified_high


def value_part(c: Candidate, cfg: SearchConfig, market: MarketSnapshot | None) -> float:
    """$/sqft against the area's trailing median, plus price-cut and DOM bonuses.

    Without a market median or the listing's sqft the base is neutral; the
    bonuses still apply because they describe the listing, not the market.
    """
    p = params(cfg)
    listing = c.listing
    base = p.value_neutral
    if market is not None and market.median_ppsf and listing.sqft:
        deviation = (listing.price / listing.sqft) / market.median_ppsf - 1
        base = _linear(deviation, -p.value_band_pct, p.value_band_pct)
    bonus = 0.0
    if "price_cut" in c.flags:
        bonus += p.value_price_cut_bonus
    if listing.days_on_market is not None and listing.days_on_market > p.value_dom_days:
        bonus += p.value_dom_bonus
    return _clamp(base + bonus)


def flood_part(c: Candidate, cfg: SearchConfig) -> float:
    p = params(cfg)
    zone = FLOODWAY if _is_floodway(c) else c.flood.fema_zone
    part = p.flood_unknown if zone is None else p.flood_by_zone.get(zone, p.flood_unknown)
    if NOTE_PRIOR_FLOODING in c.flood.notes:
        part = min(part, p.flood_prior_flooding_cap)
    return _clamp(part)


def fit_part(c: Candidate, cfg: SearchConfig) -> float:
    """Mean over the `home` minimums that are set (> 0). Meeting one scores 100;
    a shortfall scales down linearly to 0 at `fit_zero_shortfall_pct` below it."""
    p = params(cfg)
    h, listing = cfg.home, c.listing
    criteria = [
        (listing.beds, h.beds_min), (listing.baths, h.baths_min), (listing.sqft, h.sqft_min),
        (listing.lot_sqft, h.lot_sqft_min), (listing.year_built, h.year_built_min),
    ]
    scores: list[float] = []
    for value, minimum in criteria:
        if not minimum:
            continue
        if value is None:
            scores.append(p.fit_unknown)
            continue
        shortfall = max(0.0, (minimum - value) / minimum)
        scores.append(_linear(shortfall, 0.0, p.fit_zero_shortfall_pct))
    return sum(scores) / len(scores) if scores else 100.0


def carrying_cost_part(c: Candidate, cfg: SearchConfig, profile: Profile | None) -> float:
    """Monthly cost against the profile's comfortable carry level (the lower
    value of `total_housing_carry_target_monthly`). The monthly figure is the
    larger of the itemized total and the plan-basis figure, as in affordability."""
    p = params(cfg)
    cost = primary_cost(c.costs)
    targets = profile.carrying_costs.total_housing_carry_target_monthly if profile else []
    comfort = min(targets) if targets else None
    if profile is None or cost is None or not comfort or comfort <= 0:
        return p.carry_neutral
    monthly = cost.total_monthly
    if c.affordability is not None:
        monthly = max(monthly, c.affordability.plan_basis_monthly)
    return _linear(monthly / comfort, p.carry_full_ratio, p.carry_zero_ratio)


def score_parts(c: Candidate, cfg: SearchConfig, market: MarketSnapshot | None,
                profile: Profile | None) -> dict[str, float]:
    parts = {
        "schools": schools_part(c, cfg),
        "value": value_part(c, cfg, market),
        "flood": flood_part(c, cfg),
        "fit": fit_part(c, cfg),
        "carrying_cost": carrying_cost_part(c, cfg, profile),
    }
    return {k: round(v, 1) for k, v in parts.items()}


def weighted(parts: dict[str, float], cfg: SearchConfig) -> float:
    unknown = sorted(set(cfg.scoring.weights) - set(PART_NAMES))
    if unknown:
        raise ValueError(f"scoring.weights names unknown part(s) {unknown}")
    return round(sum(w * parts[k] for k, w in cfg.scoring.weights.items()), 1)


# ------------------------------------------------------------------ public


def score(c: Candidate, cfg: SearchConfig, market: MarketSnapshot | None,
          profile: Profile | None) -> float | None:
    """Weighted 0-100 score, or None when a hard disqualifier applies.

    Records the parts on `c.score_parts` and the total on `c.score`.
    """
    if disqualifier(c, cfg, profile) is not None:
        return None
    c.score_parts = score_parts(c, cfg, market, profile)
    c.score = weighted(c.score_parts, cfg)
    return c.score


def flags(c: Candidate, cfg: SearchConfig) -> list[str]:
    """Tags for the digest. Keeps flags already on the candidate (history flags
    from the pipeline) and adds the ones that follow from the candidate itself."""
    p = params(cfg)
    listing = c.listing
    out = list(c.flags)

    def add(flag: str) -> None:
        if flag not in out:
            out.append(flag)

    if listing.kind == ListingKind.SALE and listing.price > cfg.purchase.price_max:
        add("stretch")
    if c.zoning.confidence != ZoningConfidence.VERIFIED:
        add("zoning_unverified")
    cost = primary_cost(c.costs)
    if cost is not None and cost.is_jumbo:
        add("jumbo")
    if c.flood.fema_zone == "X_shaded":
        add("flood_500yr")
    if c.flood.fema_zone is None and not c.flood.in_floodway:
        add("flood_unknown")
    # Land-value comparison needs a land-value source the project does not have
    # yet; until then an old house on a stated lot is flagged for a manual look.
    if (listing.kind == ListingKind.SALE and listing.year_built is not None
            and listing.year_built < p.lot_value_year_built_before
            and listing.lot_sqft):
        add("lot_value")
    return out
