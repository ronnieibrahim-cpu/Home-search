"""Orchestration of one run. WP9 wires the stages; each stage is its own WP.

    sources -> store.upsert -> zone -> flood -> costs -> affordability
            -> rent_vs_buy -> score -> (hot alerts | weekly digest)

A stage failing for one listing must not sink the run: record it, skip that
listing, and surface the count in the digest footer.

Details:
- Every exception from a source's `fetch` becomes an outage string
  "<source>: <short reason>" and the run continues. `store.mark_missing` runs
  only for a source that returned successfully with at least one listing (an
  empty browser inbox means "no capture this run", not "everything sold").
- Scoring works on every ACTIVE listing in the store, so a source outage today
  does not empty the digest.
- With `schools.require_verified_high`, a listing whose zoning is not VERIFIED
  is kept in `RunResult.unverified` (flag `zoning_unverified`); one VERIFIED to
  a non-target high school is dropped.
- Hot alerts go out only for VERIFIED candidates, once per key and price. In a
  dry run the alert is written by `digest.send` but not recorded, so the real
  run still sends it.
- Nothing printed or logged here contains profile-derived values.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from . import config as config_mod
from . import scoring
from .config import Area, Assumptions, Profile, SearchConfig
from .digest import render as render_mod
from .digest import send as send_mod
from .enrich import flood as flood_mod
from .enrich import schools
from .finance import affordability, rent_vs_buy
from .models import (
    Candidate,
    ChangeEvent,
    Listing,
    ListingKind,
    ListingStatus,
    MarketSnapshot,
    SchoolZoning,
    ZoningConfidence,
)
from .sources import base as sources_base
from .store import Store

log = logging.getLogger(__name__)

_LOAD: Any = object()   # sentinel: "load the real one" (None means "no profile")
_REASON_MAX = 120


@dataclass
class RunResult:
    candidates: list[Candidate] = field(default_factory=list)
    rentals: list[Candidate] = field(default_factory=list)
    changes: list[ChangeEvent] = field(default_factory=list)
    outages: list[str] = field(default_factory=list)
    alerts_sent: int = 0
    # Zoning not VERIFIED while schools.require_verified_high is on: shown in a
    # separate digest section for a manual check, never silently promoted.
    unverified: list[Candidate] = field(default_factory=list)
    fetched: int = 0                                         # records sources returned
    evaluated: int = 0                                       # active sale listings scored
    dropped: dict[str, int] = field(default_factory=dict)    # disqualifier -> count
    failures: dict[str, int] = field(default_factory=dict)   # stage -> listings skipped


# ------------------------------------------------------------------ context


@dataclass
class _Ctx:
    cfg: SearchConfig
    a: Assumptions
    profile: Profile | None
    store: Store
    today: date
    boundary_dir: Path | None
    result: RunResult
    hist_flags: dict[str, set[str]] = field(default_factory=dict)
    cut_keys: set[str] = field(default_factory=set)
    # key -> (zoning, area) for every active sale / rent listing placed in an area
    sales: list[tuple[Listing, SchoolZoning, Area]] = field(default_factory=list)
    rentals: list[tuple[Listing, SchoolZoning, Area]] = field(default_factory=list)
    market: dict[str, MarketSnapshot | None] = field(default_factory=dict)
    failures: Counter = field(default_factory=Counter)

    def fail(self, stage: str, listing: Listing, exc: BaseException) -> None:
        self.failures[stage] += 1
        log.warning("stage %s failed for %s: %s", stage, listing.key, type(exc).__name__)


def _effective_cfg(cfg: SearchConfig, profile: Profile | None) -> SearchConfig:
    """search.yaml with the profile's search overrides applied (price band, beds, baths)."""
    if profile is None:
        return cfg
    ps = profile.search
    purchase: dict[str, Any] = {}
    if ps.price_min is not None:
        purchase["price_min"] = ps.price_min
    if ps.price_max is not None:
        purchase["price_max"] = ps.price_max
    home: dict[str, Any] = {}
    if ps.bedrooms_min is not None:
        home["beds_min"] = ps.bedrooms_min
    if ps.bathrooms_min is not None:
        home["baths_min"] = ps.bathrooms_min
    if not purchase and not home:
        return cfg
    return cfg.model_copy(update={
        "purchase": cfg.purchase.model_copy(update=purchase),
        "home": cfg.home.model_copy(update=home),
    })


def _resolve(cfg, assumptions, profile) -> tuple[SearchConfig, Assumptions, Profile | None]:
    cfg = cfg if cfg is not None else config_mod.load_search()
    a = assumptions if assumptions is not None else config_mod.load_assumptions()
    if profile is _LOAD:
        profile = config_mod.load_profile()
    if profile is not None and not profile.is_filled_in:
        profile = None    # the blank template would make every listing "cash short"
    return _effective_cfg(cfg, profile), a, profile


def _today(cfg: SearchConfig) -> date:
    """Today's date where the search is (schedule.timezone)."""
    return datetime.now(ZoneInfo(cfg.schedule.timezone)).date()


def _open_store(store: Store | None, db: Path | None) -> tuple[Store, bool]:
    if store is not None:
        return store, False
    return (Store(db) if db is not None else Store()), True


# ------------------------------------------------------------------ sources


def _short_reason(source_name: str, exc: BaseException) -> str:
    if isinstance(exc, NotImplementedError):
        return f"not implemented yet ({exc})" if str(exc) else "not implemented yet"
    if isinstance(exc, sources_base.SourceUnavailable):
        text = str(exc).strip()
        prefix = f"{source_name}:"
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
        return (text or "unavailable")[:_REASON_MAX]
    return f"failed ({type(exc).__name__})"


def _collect(ctx: _Ctx, sources: list) -> None:
    res = ctx.result
    fetched: list[Listing] = []
    ok: list[tuple[str, set[str]]] = []
    for src in sources:
        name = getattr(src, "name", type(src).__name__)
        try:
            listings = list(src.fetch(ctx.cfg))
        except Exception as e:  # noqa: BLE001 - any source failure is an outage
            res.outages.append(f"{name}: {_short_reason(name, e)}")
            log.warning("source %s unavailable: %s", name, type(e).__name__)
            listings = None
        res.outages.extend(str(w) for w in (getattr(src, "warnings", None) or []))
        if listings is None:
            continue
        fetched.extend(listings)
        if listings:
            ok.append((name, {lst.key for lst in listings}))
    res.fetched = len(fetched)
    changes = ctx.store.upsert(fetched) if fetched else []
    for name, keys in ok:
        changes += ctx.store.mark_missing(keys, name)
    res.changes = changes


# ------------------------------------------------------------------ history


def _history(ctx: _Ctx) -> None:
    p = scoring.params(ctx.cfg)
    window = max(p.new_days, p.recent_change_days)
    first_old: dict[str, int] = {}
    last_new: dict[str, int] = {}
    for e in ctx.store.changes_since(ctx.today - timedelta(days=window)):
        age = (ctx.today - e.at.date()).days
        flags = ctx.hist_flags.setdefault(e.key, set())
        if e.kind == "new" and age <= p.new_days:
            flags.add("new")
        if age > p.recent_change_days:
            continue
        if e.kind == "price_change" and e.old and e.new:
            try:
                first_old.setdefault(e.key, int(e.old))
                last_new[e.key] = int(e.new)
            except ValueError:
                continue
        if (e.kind == "status_change" and e.new == ListingStatus.ACTIVE.value
                and e.old in (ListingStatus.PENDING.value, ListingStatus.OFF_MARKET.value)):
            flags.add("back_on_market")
    for key, new in last_new.items():
        if new < first_old[key]:
            ctx.cut_keys.add(key)
            ctx.hist_flags.setdefault(key, set()).add("price_cut")


def _initial_flags(ctx: _Ctx, key: str) -> list[str]:
    return [f for f in scoring.HISTORY_FLAGS if f in ctx.hist_flags.get(key, set())]


# ------------------------------------------------------------------ zoning


def _place(ctx: _Ctx, kind: ListingKind) -> list[tuple[Listing, SchoolZoning, Area]]:
    out = []
    for listing in ctx.store.active(kind):
        try:
            zoning = schools.zone(listing, ctx.cfg, boundary_dir=ctx.boundary_dir)
            area = schools.area_for(listing, zoning, ctx.cfg)
        except Exception as e:  # noqa: BLE001 - per-listing stage failure
            ctx.fail("zoning", listing, e)
            continue
        if area is not None:
            out.append((listing, zoning, area))
    return out


def _verified_off_target(zoning: SchoolZoning, area: Area, cfg: SearchConfig) -> bool:
    return (zoning.confidence == ZoningConfidence.VERIFIED
            and not schools.is_target(zoning, area, cfg))


# ------------------------------------------------------------------ market


def _median_int(values: list[float]) -> int | None:
    return round(median(values)) if values else None


def market_snapshot(area_id: str, week_of: date,
                    sales: list[Listing], rentals: list[Listing],
                    cut_keys: set[str]) -> MarketSnapshot:
    """Stats over the active listings placed in one area."""
    ppsf = [s.price / s.sqft for s in sales if s.sqft]
    dom = [s.days_on_market for s in sales if s.days_on_market is not None]
    return MarketSnapshot(
        area_id=area_id,
        week_of=week_of,
        active_count=len(sales),
        median_price=_median_int([s.price for s in sales]),
        median_ppsf=round(median(ppsf), 2) if ppsf else None,
        median_dom=_median_int(dom),
        median_rent=_median_int([r.price for r in rentals]),
        price_cut_share=(round(sum(s.key in cut_keys for s in sales) / len(sales), 4)
                         if sales else None),
    )


def _week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _area_listings(ctx: _Ctx, area_id: str) -> tuple[list[Listing], list[Listing]]:
    """Active sales and rentals in an area, excluding ones verified to a non-target high."""
    sales = [lst for lst, z, ar in ctx.sales
             if ar.id == area_id and not _verified_off_target(z, ar, ctx.cfg)]
    rents = [lst for lst, z, ar in ctx.rentals
             if ar.id == area_id and not _verified_off_target(z, ar, ctx.cfg)]
    return sales, rents


def _markets(ctx: _Ctx) -> None:
    """Per area: the trailing snapshots' median $/sqft, else this run's listings."""
    weeks = scoring.params(ctx.cfg).market_weeks
    for area in ctx.cfg.areas:
        history = [s for s in ctx.store.snapshots(area.id, weeks=weeks)
                   if s.median_ppsf is not None]
        sales, rents = _area_listings(ctx, area.id)
        current = market_snapshot(area.id, _week_of(ctx.today), sales, rents, ctx.cut_keys)
        if history:
            current = current.model_copy(
                update={"median_ppsf": round(median(s.median_ppsf for s in history), 2)}
            )
        ctx.market[area.id] = current if current.median_ppsf is not None else None


# ------------------------------------------------------------------ rentals


def _rental_candidates(ctx: _Ctx) -> None:
    for listing, zoning, area in ctx.rentals:
        try:
            risk = flood_mod.assess(listing)
        except Exception as e:  # noqa: BLE001
            ctx.fail("flood", listing, e)
            continue
        c = Candidate(listing=listing, zoning=zoning, flood=risk,
                      flags=_initial_flags(ctx, listing.key))
        try:
            c.flags = scoring.flags(c, ctx.cfg)
        except Exception as e:  # noqa: BLE001
            ctx.fail("flags", listing, e)
            continue
        if not _verified_off_target(zoning, area, ctx.cfg) and not risk.in_floodway:
            ctx.result.rentals.append(c)
    ctx.result.rentals.sort(key=lambda c: (c.listing.price, c.listing.key))


def _comps(ctx: _Ctx, zoning: SchoolZoning, area: Area) -> list[Listing]:
    """Rentals in the same area; same verified high school when both are verified."""
    out = []
    for r, rz, ra in ctx.rentals:
        if ra.id != area.id:
            continue
        if (zoning.confidence == ZoningConfidence.VERIFIED
                and rz.confidence == ZoningConfidence.VERIFIED
                and not schools.same_school(zoning.high, rz.high)):
            continue
        out.append(r)
    return out


# ------------------------------------------------------------------ evaluation


def _enrich(ctx: _Ctx, listing: Listing, zoning: SchoolZoning, area: Area) -> Candidate | None:
    """Flood, costs, affordability, rent vs buy. None when a stage failed."""
    stage = "flood"
    try:
        risk = flood_mod.assess(listing)
        c = Candidate(listing=listing, zoning=zoning, flood=risk,
                      flags=_initial_flags(ctx, listing.key))
        stage = "costs"
        c.costs = affordability.costs_for(listing, area.id, risk, ctx.profile, ctx.a)
        cost = scoring.primary_cost(c.costs)
        if ctx.profile is not None and cost is not None:
            stage = "affordability"
            when = affordability.target_close_date(ctx.profile, ctx.cfg.purchase.target_year)
            c.affordability = affordability.assess(cost, listing.price, ctx.profile, ctx.a, when)
        if cost is not None:
            stage = "rent_vs_buy"
            c.rent_vs_buy = rent_vs_buy.analyze(
                listing, cost, _comps(ctx, zoning, area), ctx.profile, ctx.a
            )
    except Exception as e:  # noqa: BLE001 - per-listing stage failure
        ctx.fail(stage, listing, e)
        return None
    return c


def _score(ctx: _Ctx, c: Candidate, area: Area) -> str | None:
    """Score in place; returns the disqualifier, if any. Raises on a scoring bug."""
    reason = scoring.disqualifier(c, ctx.cfg, ctx.profile)
    if reason is not None:
        return reason
    scoring.score(c, ctx.cfg, ctx.market.get(area.id), ctx.profile)
    c.flags = scoring.flags(c, ctx.cfg)
    return None


def _evaluate(ctx: _Ctx) -> None:
    res = ctx.result
    dropped: Counter = Counter()
    require = ctx.cfg.schools.require_verified_high
    for listing, zoning, area in ctx.sales:
        res.evaluated += 1
        if _verified_off_target(zoning, area, ctx.cfg):
            dropped["non_target_high"] += 1     # no need to price or look up flood
            continue
        c = _enrich(ctx, listing, zoning, area)
        if c is None:
            continue
        try:
            reason = _score(ctx, c, area)
        except Exception as e:  # noqa: BLE001
            ctx.fail("score", listing, e)
            continue
        if reason is not None:
            dropped[reason] += 1
            continue
        verified = zoning.confidence == ZoningConfidence.VERIFIED
        if verified or (not require and schools.is_target(zoning, area, ctx.cfg)):
            res.candidates.append(c)
        else:
            res.unverified.append(c)
    order = (lambda c: (-c.score, c.listing.price, c.listing.key))
    res.candidates.sort(key=order)
    res.unverified.sort(key=order)
    res.dropped = dict(dropped)


# ------------------------------------------------------------------ alerts


def _hot_alerts(ctx: _Ctx, dry_run: bool) -> None:
    res = ctx.result
    threshold = ctx.cfg.scoring.hot_alert_threshold
    for c in res.candidates:
        if c.zoning.confidence != ZoningConfidence.VERIFIED or c.score < threshold:
            continue
        key, price = c.listing.key, c.listing.price
        if ctx.store.was_alerted(key, price):
            continue
        try:
            email = render_mod.hot_alert(c)
            send_mod.send(email, dry_run=dry_run)
        except NotImplementedError:
            res.outages.append("hot alerts: not sent, email delivery is not implemented yet")
            return
        except Exception as e:  # noqa: BLE001
            res.outages.append(f"hot alerts: one alert failed ({type(e).__name__})")
            continue
        if not dry_run:
            ctx.store.record_alert(key, price)
        res.alerts_sent += 1


# ------------------------------------------------------------------ runs


def _run(dry_run: bool, *, sources, store, cfg, assumptions, profile, today, boundary_dir,
         only, db, send_alerts: bool = True) -> tuple[_Ctx, bool]:
    cfg, a, profile = _resolve(cfg, assumptions, profile)
    st, owned = _open_store(store, db)
    ctx = _Ctx(cfg=cfg, a=a, profile=profile, store=st, today=today or _today(cfg),
               boundary_dir=boundary_dir, result=RunResult())
    try:
        _steps(ctx, dry_run, sources, only, boundary_dir, send_alerts)
    except BaseException:
        if owned:
            st.close()
        raise
    return ctx, owned


def _steps(ctx: _Ctx, dry_run: bool, sources, only, boundary_dir, send_alerts: bool) -> None:
    cfg = ctx.cfg
    if sources is None:
        sources = sources_base.enabled_sources(only=only)
    _collect(ctx, sources)
    for name in schools.missing_boundaries(cfg, boundary_dir):
        ctx.result.outages.append(
            f"school boundaries: {name} missing or unusable; zoning in that district "
            "is unverified"
        )
    _history(ctx)
    ctx.sales = _place(ctx, ListingKind.SALE)
    ctx.rentals = _place(ctx, ListingKind.RENT)
    _markets(ctx)
    _rental_candidates(ctx)
    _evaluate(ctx)
    if send_alerts:
        _hot_alerts(ctx, dry_run)
    ctx.result.failures = dict(ctx.failures)
    for stage, n in sorted(ctx.failures.items()):
        ctx.result.outages.append(
            f"pipeline: {n} listing(s) skipped after a failure at the {stage} stage"
        )


def scan(dry_run: bool = False, *, sources: list | None = None, store: Store | None = None,
         cfg: SearchConfig | None = None, assumptions: Assumptions | None = None,
         profile: Profile | None = _LOAD, today: date | None = None,
         boundary_dir: Path | None = None, only: list[str] | None = None,
         db: Path | None = None) -> RunResult:
    """Daily: collect, enrich, score, persist, send hot alerts not yet sent.

    Keyword arguments replace what is otherwise loaded: `sources` (default
    `enabled_sources(only=only)`), `store` (default the database at `db`, else
    data/state.sqlite), `cfg`, `assumptions`, `profile` (pass None for "no
    profile"), `today`, `boundary_dir` (default data/boundaries/).
    """
    ctx, owned = _run(dry_run, sources=sources, store=store, cfg=cfg, assumptions=assumptions,
                      profile=profile, today=today, boundary_dir=boundary_dir, only=only, db=db)
    if owned:
        ctx.store.close()
    return ctx.result


def _advisor(ctx: _Ctx) -> dict:
    """Inputs for the digest's advisor section (docs/FINANCE_MODEL.md §4).

    Profile-derived values: emailed only, never printed or logged.
    """
    cfg, a, profile, res = ctx.cfg, ctx.a, ctx.profile, ctx.result
    pool = res.candidates + res.unverified
    max_items = cfg.scoring.digest_max_items
    adv: dict[str, Any] = {
        "has_profile": profile is not None,
        "portfolio": rent_vs_buy.portfolio_view([c.rent_vs_buy for c in pool if c.rent_vs_buy]),
        "price_band": {"min": cfg.purchase.price_min, "max": cfg.purchase.price_max,
                       "stretch_max": cfg.purchase.stretch_max},
        "max_price": {},
        "close_date": None,
        "cash_at_close": None,
        "median_candidate_key": None,
        "median_candidate_price": None,
        "median_candidate_cash_needed": None,
        "median_candidate_reserve_months": None,
        "planning_price": None,
        "plan_rate_bracket": {},
        "unverified": res.unverified[:max_items],
        "unverified_total": len(res.unverified),
        "candidates_total": len(res.candidates),
        "dropped": dict(res.dropped),
        "stage_failures": dict(res.failures),
        "missing_boundaries": schools.missing_boundaries(cfg, ctx.boundary_dir),
        "assumptions_as_of": a.as_of,
    }
    if profile is None:
        return adv
    when = affordability.target_close_date(profile, cfg.purchase.target_year)
    adv["close_date"] = when
    adv["max_price"] = {area.id: affordability.max_price(profile, area.id, a, when)
                        for area in cfg.areas}
    adv["cash_at_close"] = round(
        affordability.cash_at(profile, when, affordability.effective_assumptions(profile, a)), 2
    )
    priced = sorted((c for c in (res.candidates or res.unverified) if c.affordability),
                    key=lambda c: (c.listing.price, c.listing.key))
    if priced:
        mid = priced[(len(priced) - 1) // 2]
        cost = scoring.primary_cost(mid.costs)
        adv["median_candidate_key"] = mid.listing.key
        adv["median_candidate_price"] = mid.listing.price
        adv["median_candidate_cash_needed"] = mid.affordability.cash_needed
        if cost is not None:
            adv["median_candidate_reserve_months"] = affordability.reserve_months(
                profile, cost.total_monthly
            )
    if profile.search.planning_price:
        adv["planning_price"] = profile.search.planning_price
        adv["plan_rate_bracket"] = affordability.plan_rate_bracket(
            profile.search.planning_price, profile, a
        )
    return adv


def digest(dry_run: bool = False, *, sources: list | None = None, store: Store | None = None,
           cfg: SearchConfig | None = None, assumptions: Assumptions | None = None,
           profile: Profile | None = _LOAD, today: date | None = None,
           boundary_dir: Path | None = None, only: list[str] | None = None,
           db: Path | None = None) -> RunResult:
    """Weekly: scan, write this week's MarketSnapshot per area, send the digest.

    `RunResult.changes` is the last seven days of changes, not just this run's.
    Unverified listings reach `render.weekly` as `advisor["unverified"]`.
    """
    ctx, owned = _run(dry_run, sources=sources, store=store, cfg=cfg, assumptions=assumptions,
                      profile=profile, today=today, boundary_dir=boundary_dir, only=only, db=db)
    try:
        res, st, cfg = ctx.result, ctx.store, ctx.cfg
        week = _week_of(ctx.today)
        for area in cfg.areas:
            sales, rents = _area_listings(ctx, area.id)
            st.save_snapshot(market_snapshot(area.id, week, sales, rents, ctx.cut_keys))
        market = [s for area in cfg.areas for s in st.snapshots(area.id)]
        res.changes = st.changes_since(ctx.today - timedelta(days=7))
        top = [c for c in res.candidates
               if c.score >= cfg.scoring.digest_min_score][: cfg.scoring.digest_max_items]
        advisor = _advisor(ctx)
        try:
            email = render_mod.weekly(top, res.rentals, res.changes, market, advisor,
                                      list(res.outages))
            send_mod.send(email, dry_run=dry_run)
        except NotImplementedError:
            res.outages.append("weekly digest: not sent, email delivery is not implemented yet")
    finally:
        if owned:
            ctx.store.close()
    return res


# ------------------------------------------------------------------ analyze


def _find(st: Store, target: str) -> Listing:
    t = target.strip()
    if not t:
        raise LookupError("give a listing key, URL or part of an address")
    found = st.get(t)
    if found is not None:
        return found
    listings = st.active(ListingKind.SALE) + st.active(ListingKind.RENT)
    by_url = [lst for lst in listings
              if t == lst.url or any(t == u for _, u in st.source_urls(lst.key))]
    if by_url:
        return by_url[0]
    needle = " ".join(t.lower().split())
    hits = [lst for lst in listings
            if needle in " ".join(f"{lst.address} {lst.city} {lst.zip}".lower().split())]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        keys = ", ".join(h.key for h in hits[:5])
        raise LookupError(f"{len(hits)} listings match {t!r}; use a listing key: {keys}")
    raise LookupError(
        f"no active listing in the database matches {t!r}; run `home-search scan` first "
        "(analyze does not fetch arbitrary URLs)"
    )


def analyze(url_or_address: str, *, store: Store | None = None,
            cfg: SearchConfig | None = None, assumptions: Assumptions | None = None,
            profile: Profile | None = _LOAD, today: date | None = None,
            boundary_dir: Path | None = None, db: Path | None = None) -> Candidate:
    """On demand: full write-up of one property already in the database.

    Accepts a listing key, a URL any source reported, or an address substring.
    Raises LookupError when nothing (or more than one listing) matches. A
    disqualified listing still comes back, scored, with a `disqualified:<reason>` flag.
    """
    cfg, a, profile = _resolve(cfg, assumptions, profile)
    st, owned = _open_store(store, db)
    try:
        listing = _find(st, url_or_address)
        ctx = _Ctx(cfg=cfg, a=a, profile=profile, store=st, today=today or _today(cfg),
                   boundary_dir=boundary_dir, result=RunResult())
        _history(ctx)
        ctx.sales = _place(ctx, ListingKind.SALE)
        ctx.rentals = _place(ctx, ListingKind.RENT)
        _markets(ctx)
        zoning = schools.zone(listing, cfg, boundary_dir=boundary_dir)
        area = schools.area_for(listing, zoning, cfg)
        if listing.kind == ListingKind.RENT or area is None:
            c = Candidate(listing=listing, zoning=zoning, flood=flood_mod.assess(listing),
                          flags=_initial_flags(ctx, listing.key))
            c.flags = scoring.flags(c, cfg)
            if area is None:
                c.flags.append("disqualified:outside_areas")
            return c
        c = _enrich(ctx, listing, zoning, area)
        if c is None:
            stage = next(iter(ctx.failures))
            raise RuntimeError(f"analysis failed at the {stage} stage")
        reason = scoring.disqualifier(c, cfg, profile)
        c.score_parts = scoring.score_parts(c, cfg, ctx.market.get(area.id), profile)
        c.score = scoring.weighted(c.score_parts, cfg) if reason is None else 0.0
        c.flags = scoring.flags(c, cfg)
        if reason is not None:
            c.flags.append(f"disqualified:{reason}")
        return c
    finally:
        if owned:
            st.close()
