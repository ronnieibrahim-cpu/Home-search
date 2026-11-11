"""Command line entry point: `home-search <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config, pipeline

# Drop reasons and score parts that follow from the private profile; never printed.
PROFILE_REASONS = {"over_hard_ceiling", "flood_tolerance", "over_stretch_max"}
PROFILE_PARTS = {"carrying_cost"}

SECRETS = ["GMAIL_USER", "GMAIL_APP_PASSWORD", "DIGEST_TO", "ALERT_GMAIL_LABEL"]


def doctor() -> int:
    """Validate config and report what is and is not set up. Prints no secret values."""
    ok = True
    try:
        cfg = config.load_search()
        zips = sum(len(a.zips) + len(a.fringe_zips) for a in cfg.areas)
        print(f"search.yaml       ok  ({len(cfg.areas)} areas, {zips} zips, "
              f"${cfg.purchase.price_min:,}-${cfg.purchase.price_max:,})")
    except Exception as e:  # noqa: BLE001 - report any config error to the user
        ok = False
        print(f"search.yaml       INVALID: {e}")
    try:
        a = config.load_assumptions()
        note = f"verified {a.as_of}" if a.as_of else "NOT YET VERIFIED (as_of is null)"
        print(f"assumptions.yaml  ok  ({note})")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"assumptions.yaml  INVALID: {e}")
    try:
        p = config.load_profile()
        if p is None:
            print("profile           missing: copy config/profile.example.yaml to "
                  "config/profile.yaml (affordability and rent-vs-buy are skipped)")
        elif not p.is_filled_in:
            print("profile           present but still the blank template")
        else:
            print("profile           ok")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"profile           INVALID: {type(e).__name__} (details withheld: private)")
    for name in SECRETS:
        print(f"{name:<20}{'set' if os.environ.get(name) else 'not set'}")
    return 0 if ok else 1


def _names(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [n.strip() for n in value.split(",") if n.strip()]


def summary(cmd: str, r: pipeline.RunResult) -> str:
    """Counts and outages only: nothing profile-derived (prices, costs, scores) is printed."""
    lines = [(
        f"{cmd} complete: {r.fetched} listings fetched, {r.evaluated} active for-sale "
        f"evaluated, {len(r.candidates)} candidates, {len(r.unverified)} unverified "
        f"(manual zoning check), {len(r.rentals)} rentals, {r.alerts_sent} hot alerts, "
        f"{len(r.outages)} outages"
    )]
    dropped: dict[str, int] = {}
    for reason, n in r.dropped.items():
        # Reasons that depend on the profile are pooled so stdout says nothing about it.
        label = "household limits" if reason in PROFILE_REASONS else reason
        dropped[label] = dropped.get(label, 0) + n
    if dropped:
        lines.append("dropped: " + ", ".join(f"{k} {v}" for k, v in sorted(dropped.items())))
    lines += [f"  outage: {o}" for o in r.outages]
    return "\n".join(lines)


def _print_analysis(c) -> None:
    """Listing facts, zoning, flood and score. Costs, affordability and the
    rent-vs-buy verdict depend on the private profile and go only to the digest."""
    lst = c.listing
    print(f"{lst.address}, {lst.city} {lst.zip}  ({lst.kind.value}, {lst.status.value})")
    print(f"  key      {lst.key}")
    print(f"  url      {lst.url}")
    facts = [f"${lst.price:,}"] + [f"{v} {k}" for k, v in (
        ("beds", lst.beds), ("baths", lst.baths), ("sqft", lst.sqft),
        ("lot sqft", lst.lot_sqft), ("built", lst.year_built), ("DOM", lst.days_on_market),
    ) if v is not None]
    print(f"  facts    {', '.join(facts)}")
    z = c.zoning
    print(f"  zoning   {z.confidence.value}: high {z.high or '?'}, middle {z.middle or '?'}, "
          f"elementary {z.elementary or '?'}")
    print(f"  flood    zone {c.flood.fema_zone or 'unknown'}"
          + (" (floodway)" if c.flood.in_floodway else ""))
    for note in c.flood.notes:
        print(f"           {note}")
    if c.score_parts:
        parts = ", ".join(f"{k} {v:g}" for k, v in c.score_parts.items()
                          if k not in PROFILE_PARTS)
        print(f"  score    parts: {parts}")
    flags = [f for f in c.flags if f.removeprefix("disqualified:") not in PROFILE_REASONS]
    if flags:
        print(f"  flags    {', '.join(flags)}")
    print("  costs, affordability and rent vs buy are in the emailed digest (profile-derived)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="home-search", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check config, profile and secrets")
    for name, text in [("scan", "collect and score; send hot alerts"),
                       ("digest", "scan, then send the weekly digest")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("--dry-run", action="store_true", help="write emails to out/ instead")
        p.add_argument("--sources", metavar="NAME[,NAME]",
                       help="only these sources (har_browser, email_alerts, har_web, redfin_csv)")
        p.add_argument("--db", type=Path, metavar="PATH", help="SQLite database to use")
    p = sub.add_parser("analyze", help="full write-up of one listing URL or address")
    p.add_argument("target")
    p.add_argument("--db", type=Path, metavar="PATH", help="SQLite database to use")
    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        return doctor()
    try:
        if args.cmd in ("scan", "digest"):
            run = pipeline.scan if args.cmd == "scan" else pipeline.digest
            result = run(dry_run=args.dry_run, only=_names(args.sources), db=args.db)
            print(summary(args.cmd, result))
        elif args.cmd == "analyze":
            _print_analysis(pipeline.analyze(args.target, db=args.db))
    except NotImplementedError as e:
        print(f"not implemented yet: {e} (see docs/TASKS.md)", file=sys.stderr)
        return 2
    except (LookupError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
