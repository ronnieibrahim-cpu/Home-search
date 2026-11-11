"""Build the digest and hot-alert emails (HTML + plain text). WP10.

Section order for the weekly digest:
  1. Advisor summary: rent-vs-buy headline, max affordable price, savings runway to 2027
  2. Top candidates (ranked, with score breakdown, monthly cost, zoning, flood)
  3. What changed: new, price cuts, went pending
  4. Rentals worth a look
  5. Market trend per area (from MarketSnapshot history)
  6. Zoning-unverified listings (manual check needed)
  7. Footer: source outages, assumptions as_of date, flood and zoning caveats

How it is built: each section is first reduced to a plain view model (strings,
lists and dicts, no markup). The HTML part renders that model through Jinja2
templates with autoescape on; the plain-text part renders the same model in
Python. Both therefore carry the same information in the same order.

Every listing-derived string is untrusted (it comes from listing sites and
alert emails). It is only ever escaped by the template, never marked safe, and
a listing URL becomes a link only when it is an absolute http(s) URL.

The output contains profile-derived numbers: it goes to `send` and nowhere
else. Nothing here logs or prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..models import Candidate, ChangeEvent, ListingKind, ListingStatus, MarketSnapshot

TEMPLATES = Path(__file__).parent / "templates"

# Listing text is cut to this many characters in the card.
DESCRIPTION_MAX = 280
# Changes shown per group and rentals shown, before "and N more".
CHANGES_PER_GROUP_MAX = 20
RENTALS_MAX = 15
SUBJECT_ADDRESS_MAX = 80

LIMITS_TEXT = (
    "Estimates, not quotes. Insurance and tax figures must be replaced with real quotes "
    "and the HCAD record before an offer. This tool does not replace a lender "
    "pre-approval, an inspection, a seller's disclosure review, or a CPA."
)
FLOOD_CAVEAT = (
    "Flood zones are FEMA's mapped hazard, not a flood history: zone X does not mean the "
    "house has never flooded, and many Houston homes outside the 100-year zone flooded in "
    "2017. Request the seller's disclosure (in Texas it asks about previous flooding and "
    "flood claims) and the flood insurance history before an offer."
)
ZONING_CAVEAT = (
    "Zoning is marked verified only when the listing's coordinates fall inside the "
    "district's boundary map for the school year shown. Boundaries change and coordinates "
    "can be off, so confirm the address with the district's own address lookup (HISD, "
    "SBISD) before an offer. A school named in the listing is never treated as verified."
)
UNVERIFIED_NOTE = (
    "These listings are in the search areas but their high school is not confirmed by a "
    "boundary map. Check each address with the district's address lookup (HISD or SBISD "
    "school finder) before relying on the school."
)

PART_LABELS = {
    "schools": "Schools",
    "value": "Value",
    "flood": "Flood",
    "fit": "Fit",
    "carrying_cost": "Carrying cost",
}
FLAG_LABELS = {
    "new": "New",
    "price_cut": "Price cut",
    "back_on_market": "Back on market",
    "stretch": "Stretch (over budget)",
    "zoning_unverified": "Zoning unverified",
    "jumbo": "Jumbo loan",
    "flood_500yr": "500-year flood zone",
    "flood_unknown": "Flood zone unknown",
    "lot_value": "Older house: check lot value",
}
ZONE_LABELS = {
    "X": "X (minimal mapped hazard)",
    "X_shaded": "X shaded (0.2% / 500-year)",
    "AE": "AE (1% / 100-year, flood insurance required)",
    "floodway": "Floodway",
}
CONFIDENCE_LABELS = {
    "verified": "Verified from the district boundary map",
    "listing_claim": "Listing claim only (not verified)",
    "unknown": "Unknown (not verified)",
}
VERDICT_LABELS = {"buy": "Buy", "rent": "Rent", "close_call": "Close call"}
DROP_LABELS = {
    "floodway": "in a floodway",
    "flood_tolerance": "flood zone beyond your tolerance",
    "outside_areas": "outside the search areas",
    "non_target_high": "zoned to a non-target high school",
    "excluded_keyword": "excluded keyword",
    "over_stretch_max": "priced above the stretch maximum",
    "over_hard_ceiling": "monthly cost above your hard ceiling",
}
STATUS_LABELS = {
    "active": "active",
    "pending": "pending",
    "sold": "sold",
    "off_market": "off market",
}
COST_ITEMS = (
    ("principal_interest", "Principal & interest"),
    ("property_tax", "Property tax"),
    ("homeowners_insurance", "Homeowners insurance"),
    ("flood_insurance", "Flood insurance"),
    ("pmi", "PMI"),
    ("hoa", "HOA"),
    ("maintenance", "Maintenance"),
)


@dataclass
class Email:
    subject: str
    html: str
    text: str


# ------------------------------------------------------------------ formatting


def _money(x: float | None) -> str:
    """$1,234,567, whole dollars; "n/a" for None."""
    if x is None:
        return "n/a"
    v = round(float(x))
    return f"-${-v:,}" if v < 0 else f"${v:,}"


def _signed_money(x: float) -> str:
    v = round(float(x))
    return f"+${v:,}" if v > 0 else ("$0" if v == 0 else f"-${-v:,}")


def _pct(rate: float | None) -> str:
    """A rate (0.0625) as 6.25%."""
    return "n/a" if rate is None else f"{rate * 100:.2f}%"


def _share(x: float | None) -> str:
    """A share (0.4) as 40%."""
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _num(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{round(x):,}"


def _date(d: date | datetime | None) -> str:
    if d is None:
        return "n/a"
    return f"{d:%b} {d.day}, {d.year}"


def _clean(s: object) -> str:
    """Untrusted text on one line: control characters and runs of whitespace collapsed."""
    return " ".join(str(s).split()) if s is not None else ""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def safe_url(url: str | None) -> str | None:
    """The URL when it is an absolute http(s) URL with a host and nothing odd in it."""
    if not url:
        return None
    u = url.strip()
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in u):
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return u


def _label(code: str, labels: dict[str, str]) -> str:
    if code in labels:
        return labels[code]
    if code.startswith("disqualified:"):
        reason = code.removeprefix("disqualified:")
        return "Disqualified: " + DROP_LABELS.get(reason, reason.replace("_", " "))
    return code.replace("_", " ").capitalize()


def scenario_label(scenario_id: str, short: bool = False) -> str:
    """Reader-facing name of a loan scenario id."""
    if scenario_id == "plan":
        return "Plan" if short else "Your plan"
    if scenario_id == "plan_market":
        return "Plan at market rate" if short else "Your plan at today's market rate"
    m = re.fullmatch(r"([a-z]+)_(\d+)", scenario_id)
    if m:
        return f"{m.group(1).capitalize()} {int(m.group(2))}% down"
    return scenario_id.replace("_", " ").capitalize()


@lru_cache(maxsize=1)
def _area_names() -> dict[str, str]:
    """Area id -> display name from search.yaml; empty if it cannot be read."""
    try:
        from ..config import load_search

        return {a.id: a.name for a in load_search().areas}
    except Exception:  # noqa: BLE001 - names are cosmetic; fall back to ids
        return {}


def _area_name(area_id: str) -> str:
    return _area_names().get(area_id) or area_id.replace("_", " ").title()


def _primary_cost(c: Candidate):
    from ..scoring import primary_cost

    return primary_cost(c.costs)


# ------------------------------------------------------------------ view models


def _facts(c: Candidate) -> str:
    lst = c.listing
    out = []
    if lst.beds is not None:
        out.append(f"{_num(lst.beds)} bd")
    if lst.baths is not None:
        out.append(f"{_num(lst.baths)} ba")
    if lst.sqft:
        out.append(f"{lst.sqft:,} sqft")
        if lst.kind == ListingKind.SALE and lst.price:
            out.append(f"{_money(lst.price / lst.sqft)}/sqft")
    if lst.lot_sqft:
        out.append(f"lot {lst.lot_sqft:,} sqft")
    if lst.year_built:
        out.append(f"built {lst.year_built}")
    if lst.days_on_market is not None:
        out.append(f"{lst.days_on_market} days on market")
    if lst.hoa_monthly:
        out.append(f"HOA {_money(lst.hoa_monthly)}/mo")
    return " · ".join(out)


def _zoning_view(c: Candidate) -> dict:
    z = c.zoning
    names = [_clean(n) for n in (z.elementary, z.middle, z.high) if n]
    source = None
    if not names and c.listing.har_schools:
        names = [_clean(v) for v in c.listing.har_schools.values() if v]
        source = "as the listing site reports them"
    ratings = ", ".join(f"{_clean(k)} {v}/10" for k, v in z.ratings.items())
    return {
        "schools": " · ".join(names) or "schools not identified",
        "source": source,
        "confidence": _label(z.confidence.value, CONFIDENCE_LABELS),
        "verified": z.confidence.value == "verified",
        "district": _clean(z.district) or None,
        "vintage": (f"boundary map {_clean(z.boundary_vintage)}" if z.boundary_vintage
                    else "no boundary map match"),
        "ratings": ratings or None,
    }


def _flood_view(c: Candidate) -> dict:
    f = c.flood
    zone = "floodway" if f.in_floodway else f.fema_zone
    return {
        "zone": ZONE_LABELS.get(zone, _clean(zone)) if zone else "Unknown (not determined)",
        "notes": [_clean(n) for n in f.notes],
    }


def _cost_view(c: Candidate) -> dict | None:
    cost = _primary_cost(c)
    if cost is None:
        return None
    price = c.listing.price
    down = cost.down_payment / price if price else 0.0
    bits = [_pct(cost.rate), f"{down * 100:.0f}% down {_money(cost.down_payment)}"]
    if cost.is_jumbo:
        bits.append("jumbo")
    items = [(label, _money(getattr(cost, attr))) for attr, label in COST_ITEMS
             if attr == "principal_interest" or getattr(cost, attr)]
    return {
        "scenario": scenario_label(cost.scenario_id),
        "terms": ", ".join(bits),
        "total": _money(cost.total_monthly),
        "items": items,
    }


def _afford_view(c: Candidate) -> dict | None:
    a = c.affordability
    if a is None:
        return None
    return {
        "plan_basis": _money(a.plan_basis_monthly),
        "cash_needed": _money(a.cash_needed),
        "cash_available": _money(a.cash_available_at_purchase),
        "dti": _share(a.dti_back_end) if a.dti_back_end != float("inf") else "n/a",
        "within_comfort": a.within_comfort,
        "blockers": [_clean(b) for b in a.blockers],
    }


def _rvb_view(c: Candidate) -> dict | None:
    r = c.rent_vs_buy
    if r is None:
        return None
    if r.rent_source.startswith("comp:"):
        n = r.rent_source.removeprefix("comp:").split()[0]
        source = f"median of {n} comparable rental{'' if n == '1' else 's'}"
    else:
        source = "fallback price-to-rent ratio"
    be = (f"break-even year {r.breakeven_year}" if r.breakeven_year is not None
          else "no break-even in the tested horizons")
    return {
        "verdict": VERDICT_LABELS.get(r.verdict, _clean(r.verdict)),
        "verdict_code": r.verdict,
        "summary": (f"{r.horizon_years}-year advantage of buying "
                    f"{_signed_money(r.npv_buy_minus_rent)} (today's dollars), {be}; "
                    f"rent equivalent {_money(r.rent_equivalent_monthly)}/mo from the {source}"),
        "rationale": [_clean(s) for s in r.rationale],
    }


def card(c: Candidate, rank: int | None = None) -> dict:
    """View model for one listing card."""
    lst = c.listing
    url = safe_url(lst.url)
    rent = lst.kind == ListingKind.RENT
    status = None if lst.status == ListingStatus.ACTIVE else STATUS_LABELS.get(lst.status.value)
    desc = _clean(lst.description)
    return {
        "rank": rank,
        "address": _clean(lst.address) or "(no address)",
        "place": _clean(f"{lst.city} {lst.zip}"),
        "url": url,
        "raw_url": None if url else (_truncate(_clean(lst.url), 200) or None),
        "price": _money(lst.price) + ("/mo" if rent else ""),
        "status": status,
        "facts": _facts(c),
        "description": _truncate(desc, DESCRIPTION_MAX) if desc else None,
        "score": f"{c.score:.1f}" if c.score_parts else None,
        "parts": [(PART_LABELS.get(k, k), f"{c.score_parts[k]:g}")
                  for k in (*PART_LABELS, *[p for p in c.score_parts if p not in PART_LABELS])
                  if k in c.score_parts],
        "flags": [_label(f, FLAG_LABELS) for f in c.flags],
        "zoning": _zoning_view(c),
        "flood": _flood_view(c),
        "cost": None if rent else _cost_view(c),
        "afford": None if rent else _afford_view(c),
        "rvb": None if rent else _rvb_view(c),
    }


def _majority(portfolio: dict | None) -> tuple[str | None, str]:
    """(verdict code, headline such as "buying wins on 2 of 3")."""
    if not portfolio or not portfolio.get("count"):
        return None, "no rent-vs-buy verdict yet"
    n = portfolio["count"]
    v = portfolio.get("verdicts") or {}
    order = ("buy", "close_call", "rent")
    top = max(order, key=lambda k: (v.get(k, 0), -order.index(k)))
    k = v.get(top, 0)
    phrase = {"buy": "buying wins", "rent": "renting wins", "close_call": "close call"}[top]
    return top, f"{phrase} on {k} of {n}"


def _band_note(value: int, band: dict) -> str:
    lo, hi = band.get("min"), band.get("max")
    if lo is not None and value < lo:
        return "below band"
    if hi is not None and value <= hi:
        return "in band"
    return "above band"


def _summary(adv: dict, has_candidates: bool) -> list[dict]:
    """Section 1: 3-6 short items, answer first. Each item: lead, text, sub, tone."""
    items: list[dict] = []
    missing = list(adv.get("missing_boundaries") or [])
    total = adv.get("candidates_total")
    n_unv = adv.get("unverified_total") or 0
    if missing and not total and not has_candidates:
        items.append({
            "lead": "No verified candidates yet.",
            "text": (f"The school boundary file{'s' if len(missing) > 1 else ''} "
                     f"{', '.join(missing)} {'are' if len(missing) > 1 else 'is'} missing, so no "
                     "listing's school zoning can be confirmed. "
                     + (f"The {n_unv} listing{'s' if n_unv != 1 else ''} under "
                        "\"Zoning unverified\" below are NOT verified: check each address with "
                        "the district's address lookup before relying on the school."
                        if n_unv else "No unverified listings matched either.")),
            "sub": [], "tone": "warn",
        })
    elif missing:
        items.append({
            "lead": "Some zoning is unverified.",
            "text": (f"Missing boundary file(s): {', '.join(missing)}. Listings in that "
                     "district appear under \"Zoning unverified\" until the file is added."),
            "sub": [], "tone": "warn",
        })

    pf = adv.get("portfolio") or {}
    code, headline = _majority(pf)
    if code is None:
        items.append({"lead": "Buy or rent:", "text": "no listing could be priced this week, "
                      "so there is no rent-vs-buy verdict.", "sub": [], "tone": None})
    else:
        n = pf.get("count") or 0
        v = pf.get("verdicts") or {}
        parts = [(f"{headline[0].upper()}{headline[1:]} listings priced "
                  f"(buy {v.get('buy', 0)}, close call {v.get('close_call', 0)}, "
                  f"rent {v.get('rent', 0)}).")]
        be = pf.get("median_breakeven_year")
        never = pf.get("never_break_even") or 0
        if be is not None and not never:
            parts.append(f"Median break-even: buying comes out ahead if you sell in year "
                         f"{_num(be)} or later.")
        elif be is not None:
            parts.append(f"{n - never} of {n} break even within the horizons tested (median "
                         f"year {_num(be)}); {never} never do.")
        else:
            parts.append("None break even within the horizons tested.")
        if pf.get("median_npv") is not None:
            parts.append(f"Median gain from buying instead of renting: "
                         f"{_signed_money(pf['median_npv'])} in today's dollars.")
        sw = pf.get("swing_factor")
        if sw:
            label = _clean(sw.get("label") or sw.get("dimension"))
            share = sw.get("flip_share") or 0
            if share > 0:
                parts.append(f"Swing factor: {label}; across its tested range it flips the "
                             f"verdict in {_share(share)} of cases.")
            else:
                parts.append(f"No tested assumption flips the verdict; {label} moves it most.")
        items.append({"lead": "Buy or rent:", "text": " ".join(parts), "sub": [],
                      "tone": code})

    if not adv.get("has_profile"):
        items.append({
            "lead": "No household profile loaded:",
            "text": "max price, runway and the plan rate bracket are skipped; costs use the "
                    "standard loan scenarios and the tax deduction is not modeled.",
            "sub": [], "tone": None,
        })
        return items

    band = adv.get("price_band") or {}
    max_price = adv.get("max_price") or {}
    if max_price:
        sub = []
        for area_id, by_scenario in max_price.items():
            bits = [f"{scenario_label(s, short=True)} {_money(v)} ({_band_note(v, band)})"
                    for s, v in (by_scenario or {}).items()]
            if bits:
                sub.append(f"{_area_name(area_id)}: " + "; ".join(bits))
        band_txt = (f"{_money(band.get('min'))}–{_money(band.get('max'))}"
                    + (f", stretch to {_money(band['stretch_max'])}"
                       if band.get("stretch_max") else ""))
        items.append({"lead": "What you can carry:",
                      "text": f"highest price that passes every check, per loan scenario, "
                              f"against your {band_txt} band.",
                      "sub": sub, "tone": None})

    cash = adv.get("cash_at_close")
    if cash is not None:
        when = adv.get("close_date")
        text = f"projected cash at close ({_date(when)}) is {_money(cash)}"
        need = adv.get("median_candidate_cash_needed")
        if need is not None:
            gap = cash - need
            text += (f"; the median candidate ({_money(adv.get('median_candidate_price'))}) "
                     f"needs {_money(need)} down plus closing, "
                     + (f"leaving {_money(gap)} spare." if gap >= 0
                        else f"a shortfall of {_money(-gap)}."))
        else:
            text += "; no priced candidate to compare against this week."
        months = adv.get("median_candidate_reserve_months")
        if months is not None:
            text += (f" Other assets cover {_num(months)} months of that home's housing cost "
                     "(reserves only, never spent on the purchase).")
        items.append({"lead": "Runway:", "text": text, "sub": [], "tone": None})

    bracket = adv.get("plan_rate_bracket") or {}
    if bracket:
        rates = sorted(bracket.items())
        pieces = [f"{_money(pi)}/mo at {_pct(r)}" for r, pi in rates]
        joined = pieces[0] if len(pieces) == 1 else ", ".join(pieces[:-1]) + " and " + pieces[-1]
        planning = adv.get("planning_price")
        at = f" at the planning price of {_money(planning)}" if planning else ""
        items.append({"lead": "Rate bracket:",
                      "text": f"your plan's principal and interest{at} is {joined}.",
                      "sub": [], "tone": None})
    return items


def _change_groups(changes: list[ChangeEvent], index: dict[str, Candidate]) -> dict:
    """Section 3, limited to listings that appear in this digest."""
    shown = [e for e in changes if e.key in index]
    other = len({e.key for e in changes} - set(index))
    by_key: dict[str, list[ChangeEvent]] = {}
    for e in sorted(shown, key=lambda e: e.at):
        by_key.setdefault(e.key, []).append(e)

    groups: dict[str, list[dict]] = {
        "New listings": [], "Price cuts": [], "Price increases": [],
        "Went pending or off market": [], "Back on market": [],
    }

    def row(key: str, detail: str) -> dict:
        c = index[key]
        return {"address": _clean(c.listing.address), "url": safe_url(c.listing.url),
                "detail": detail}

    for key, events in by_key.items():
        rent = key.startswith(ListingKind.RENT.value + "|")
        suffix = "/mo" if rent else ""
        kinds = [e.kind for e in events]
        if "new" in kinds:
            c = index[key]
            groups["New listings"].append(row(key, _money(c.listing.price) + suffix))
        prices = [e for e in events if e.kind == "price_change"]
        if prices and "new" not in kinds:
            try:
                old, new = int(prices[0].old or 0), int(prices[-1].new or 0)
            except ValueError:
                old = new = 0
            if old and new and new != old:
                detail = (f"{_money(old)}{suffix} → {_money(new)}{suffix} "
                          f"({_signed_money(new - old)}, {(new - old) / old * 100:+.1f}%)")
                groups["Price cuts" if new < old else "Price increases"].append(row(key, detail))
        for e in events:
            if e.kind in ("status_change", "removed"):
                new_s = STATUS_LABELS.get(e.new or "", _clean(e.new))
                old_s = STATUS_LABELS.get(e.old or "", _clean(e.old))
                if e.new == ListingStatus.ACTIVE.value:
                    groups["Back on market"].append(row(key, f"was {old_s}"))
                else:
                    groups["Went pending or off market"].append(
                        row(key, f"{old_s} → {new_s} ({_date(e.at)})"))
    out = []
    for title, rows in groups.items():
        if rows:
            more = max(0, len(rows) - CHANGES_PER_GROUP_MAX)
            out.append({"title": title, "rows": rows[:CHANGES_PER_GROUP_MAX], "more": more})
    return {"groups": out, "other": other}


def _delta(new: float | None, old: float | None, fmt) -> str | None:
    if new is None or old is None:
        return None
    return fmt(new - old)


def _market(market: list[MarketSnapshot]) -> list[dict]:
    """Section 5: per area, latest snapshot and change vs the oldest one listed."""
    by_area: dict[str, list[MarketSnapshot]] = {}
    for s in market:
        by_area.setdefault(s.area_id, []).append(s)
    out = []
    for area_id, snaps in by_area.items():
        snaps = sorted(snaps, key=lambda s: s.week_of)
        last, first = snaps[-1], snaps[0]
        single = len({s.week_of for s in snaps}) == 1

        def pct_change(new, old):
            if new is None or old in (None, 0):
                return None
            return f"{_signed_money(new - old)}, {(new - old) / old * 100:+.1f}%"

        rows = [
            ("Active listings", str(last.active_count),
             None if single else f"{last.active_count - first.active_count:+d}"),
            ("Median list price", _money(last.median_price),
             None if single else pct_change(last.median_price, first.median_price)),
            ("Median $/sqft", (f"{_money(last.median_ppsf)}" if last.median_ppsf is not None
                               else "n/a"),
             None if single else _delta(last.median_ppsf, first.median_ppsf, _signed_money)),
            ("Median days on market",
             str(last.median_dom) if last.median_dom is not None else "n/a",
             None if single else _delta(last.median_dom, first.median_dom,
                                        lambda d: f"{d:+.0f} days")),
            ("Median rent", (_money(last.median_rent) + "/mo") if last.median_rent else "n/a",
             None if single else pct_change(last.median_rent, first.median_rent)),
            ("Share with a price cut", _share(last.price_cut_share),
             None if single else _delta(last.price_cut_share, first.price_cut_share,
                                        lambda d: f"{d * 100:+.0f} pts")),
        ]
        out.append({
            "name": _area_name(area_id),
            "week": _date(last.week_of),
            "since": None if single else _date(first.week_of),
            "weeks": len(snaps),
            "single": single,
            "rows": rows,
        })
    return out


def _footer(adv: dict, outages: list[str]) -> dict:
    dropped = adv.get("dropped") or {}
    failures = adv.get("stage_failures") or {}
    as_of = adv.get("assumptions_as_of")
    return {
        "outages": [_clean(o) for o in outages],
        "missing_boundaries": list(adv.get("missing_boundaries") or []),
        "as_of": (f"Assumptions as of {_date(as_of)}." if as_of
                  else "Assumptions not yet verified (no as_of date)."),
        "dropped": [f"{DROP_LABELS.get(k, k.replace('_', ' '))}: {v}"
                    for k, v in sorted(dropped.items())],
        "failures": [f"{k}: {v}" for k, v in sorted(failures.items())],
        "limits": LIMITS_TEXT,
        "flood": FLOOD_CAVEAT,
        "zoning": ZONING_CAVEAT,
    }


def _week_of(market: list[MarketSnapshot]) -> date:
    if market:
        return max(s.week_of for s in market)
    today = datetime.now().astimezone().date()
    return today - timedelta(days=today.weekday())


# ------------------------------------------------------------------ rendering


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _text_card(cv: dict) -> list[str]:
    head = f"{cv['rank']}. " if cv.get("rank") else "* "
    pad = " " * len(head)
    out = [f"{head}{cv['address']}, {cv['place']}"]
    status = f" ({cv['status']})" if cv["status"] else ""
    out.append(f"{pad}{cv['price']}{status}" + (f" | {cv['facts']}" if cv["facts"] else ""))
    if cv["url"]:
        out.append(f"{pad}{cv['url']}")
    elif cv["raw_url"]:
        out.append(f"{pad}Listing link (not http/https, not linked): {cv['raw_url']}")
    if cv["score"]:
        parts = ", ".join(f"{k.lower()} {v}" for k, v in cv["parts"])
        out.append(f"{pad}Score {cv['score']}/100: {parts}")
    if cv["flags"]:
        out.append(f"{pad}Tags: {', '.join(cv['flags'])}")
    z = cv["zoning"]
    district = f"{z['district']}, " if z["district"] else ""
    source = f" ({z['source']})" if z["source"] else ""
    out.append(f"{pad}Zoning: {z['schools']}{source}")
    out.append(f"{pad}  {z['confidence']}; {district}{z['vintage']}")
    if z["ratings"]:
        out.append(f"{pad}  Ratings: {z['ratings']}")
    f = cv["flood"]
    out.append(f"{pad}Flood zone: {f['zone']}")
    out += [f"{pad}  - {n}" for n in f["notes"]]
    if cv["cost"]:
        c = cv["cost"]
        out.append(f"{pad}Monthly cost {c['total']}: {c['scenario']}, {c['terms']}")
        out.append(f"{pad}  " + " | ".join(f"{k} {v}" for k, v in c["items"]))
    if cv["afford"]:
        a = cv["afford"]
        ok = "within your comfort level" if a["within_comfort"] else "NOT within your comfort level"
        out.append(f"{pad}Your plan: {a['plan_basis']}/mo plan basis; cash needed "
                   f"{a['cash_needed']} of {a['cash_available']} projected; DTI {a['dti']}; {ok}")
        out += [f"{pad}  ! {b}" for b in a["blockers"]]
    if cv["rvb"]:
        r = cv["rvb"]
        out.append(f"{pad}Rent vs buy: {r['verdict'].upper()}. {r['summary']}.")
        out += [f"{pad}  - {s}" for s in r["rationale"]]
    if cv["description"]:
        out.append(f"{pad}Listing says: \"{cv['description']}\"")
    return out


def _heading(title: str) -> list[str]:
    return ["", title.upper(), "=" * len(title)]


def _text_caveats(foot: dict) -> list[str]:
    return [f"- {foot['limits']}", f"- {foot['flood']}", f"- {foot['zoning']}"]


def _weekly_text(m: dict) -> str:
    out = [m["subject"], ""]
    if m.get("banner"):
        out += [m["banner"], ""]
    out += _heading("1. The short answer")
    for it in m["summary"]:
        mark = "!! " if it["tone"] == "warn" else "- "
        out.append(f"{mark}{it['lead']} {it['text']}")
        out += [f"    - {s}" for s in it["sub"]]

    out += _heading("2. Top candidates")
    if m["cards_note"]:
        out.append(m["cards_note"])
    for cv in m["cards"]:
        out.append("")
        out += _text_card(cv)

    out += _heading("3. What changed this week")
    ch = m["changes"]
    if not ch["groups"]:
        out.append("No changes to the listings in this digest in the last 7 days.")
    for g in ch["groups"]:
        out.append(f"{g['title']}:")
        for r in g["rows"]:
            link = f" <{r['url']}>" if r["url"] else ""
            out.append(f"  - {r['address']}: {r['detail']}{link}")
        if g["more"]:
            out.append(f"  ... and {g['more']} more")
    if ch["other"]:
        out.append(f"({ch['other']} other listing(s) changed that are not in this digest.)")

    out += _heading("4. Rentals worth a look")
    if not m["rentals"]:
        out.append("No rentals in the search areas this week.")
    for cv in m["rentals"]:
        out.append("")
        out += _text_card(cv)
    if m["rentals_more"]:
        out.append(f"... and {m['rentals_more']} more rentals.")

    out += _heading("5. Market trend")
    if not m["market"]:
        out.append("No market snapshots yet.")
    for area in m["market"]:
        since = ("first week of data" if area["single"]
                 else f"change since {area['since']}, {area['weeks']} weeks of data")
        out.append(f"{area['name']}, week of {area['week']} ({since}):")
        for label, value, delta in area["rows"]:
            out.append(f"  - {label}: {value}" + (f" ({delta})" if delta else ""))

    out += _heading("6. Zoning unverified: check by hand")
    out.append(m["unverified_note"])
    if not m["unverified"]:
        out.append("None this week.")
    for cv in m["unverified"]:
        out.append("")
        out += _text_card(cv)

    out += _heading("7. Notes and limits")
    out += _text_footer(m["footer"])
    return "\n".join(out).rstrip() + "\n"


def _text_footer(foot: dict) -> list[str]:
    out = []
    if foot["outages"]:
        out.append("Source outages and warnings:")
        out += [f"  - {o}" for o in foot["outages"]]
    else:
        out.append("No source outages.")
    if foot["dropped"]:
        out.append("Dropped this run: " + "; ".join(foot["dropped"]) + ".")
    if foot["failures"]:
        out.append("Listings skipped after a stage failure: " + "; ".join(foot["failures"]) + ".")
    out.append(foot["as_of"])
    out += _text_caveats(foot)
    return out


def _subject_text(s: str) -> str:
    return _truncate(_clean(s), 200)


def weekly(candidates: list[Candidate], rentals: list[Candidate], changes: list[ChangeEvent],
           market: list[MarketSnapshot], advisor: dict, outages: list[str]) -> Email:
    adv = advisor or {}
    candidates = list(candidates or [])
    rentals = list(rentals or [])
    unverified = list(adv.get("unverified") or [])
    market = list(market or [])

    total = adv.get("candidates_total")
    total = len(candidates) if total is None else total
    n_unv = adv.get("unverified_total")
    n_unv = len(unverified) if n_unv is None else n_unv
    _, headline = _majority(adv.get("portfolio"))
    week = _week_of(market)
    subject = (f"Home search: {total} candidates"
               + (f" + {n_unv} unverified" if n_unv else "")
               + f", {headline} (week of {_date(week)})")

    index: dict[str, Candidate] = {}
    for c in [*candidates, *rentals, *unverified]:
        index.setdefault(c.listing.key, c)

    if candidates:
        note = (f"Showing {len(candidates)} of {total} verified candidates; the rest scored "
                "below the digest minimum." if total > len(candidates) else None)
    elif adv.get("missing_boundaries") and not total:
        note = ("None: zoning cannot be verified until the boundary files are added. "
                "See \"Zoning unverified\" below.")
    elif total:
        note = f"{total} verified candidate(s), none scored high enough for the digest."
    else:
        note = "No verified candidate this week."

    model = {
        "subject": _subject_text(subject),
        "banner": adv.get("banner"),
        "summary": _summary(adv, bool(candidates)),
        "cards": [card(c, i + 1) for i, c in enumerate(candidates)],
        "cards_note": note,
        "changes": _change_groups(list(changes or []), index),
        "rentals": [card(c) for c in rentals[:RENTALS_MAX]],
        "rentals_more": max(0, len(rentals) - RENTALS_MAX),
        "market": _market(market),
        "unverified": [card(c) for c in unverified],
        "unverified_note": (UNVERIFIED_NOTE
                            + (f" Showing {len(unverified)} of {n_unv}."
                               if n_unv > len(unverified) else "")),
        "footer": _footer(adv, list(outages or [])),
    }
    model["preheader"] = " ".join(f"{i['lead']} {i['text']}" for i in model["summary"][:1])
    html = _env().get_template("digest.html.j2").render(m=model)
    return Email(subject=model["subject"], html=html, text=_weekly_text(model))


def hot_alert(candidate: Candidate) -> Email:
    c = candidate
    score = f"{c.score:.1f}".removesuffix(".0")
    address = _truncate(_clean(c.listing.address), SUBJECT_ADDRESS_MAX)
    subject = _subject_text(f"Hot listing: {address} {_money(c.listing.price)} (score {score})")
    cv = card(c)
    foot = _footer({}, [])
    intro = (f"Scored {score}/100, at or above the hot-alert threshold. Zoning, flood and "
             "costs are below; check the caveats before acting.")
    model = {"subject": subject, "intro": intro, "card": cv, "footer": foot,
             "preheader": f"{cv['price']} · {cv['facts']}"}
    html = _env().get_template("hot_alert.html.j2").render(m=model)
    text = "\n".join([subject, "", intro, "", *_text_card(cv), "", "NOTES AND LIMITS",
                      *_text_caveats(foot)]) + "\n"
    return Email(subject=subject, html=html, text=text)

