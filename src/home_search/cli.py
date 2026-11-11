"""Command line entry point: `home-search <command>`."""

from __future__ import annotations

import argparse
import os
import sys

from . import config, pipeline

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="home-search", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check config, profile and secrets")
    for name, text in [("scan", "collect and score; send hot alerts"),
                       ("digest", "scan, then send the weekly digest")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("--dry-run", action="store_true", help="write emails to out/ instead")
    p = sub.add_parser("analyze", help="full write-up of one listing URL or address")
    p.add_argument("target")
    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        return doctor()
    try:
        if args.cmd == "scan":
            pipeline.scan(dry_run=args.dry_run)
        elif args.cmd == "digest":
            pipeline.digest(dry_run=args.dry_run)
        elif args.cmd == "analyze":
            pipeline.analyze(args.target)
    except NotImplementedError as e:
        print(f"not implemented yet: {e} (see docs/TASKS.md)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
