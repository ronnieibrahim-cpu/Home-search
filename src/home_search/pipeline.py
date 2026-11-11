"""Orchestration of one run. WP9 wires the stages; each stage is its own WP.

    sources -> store.upsert -> zone -> flood -> costs -> affordability
            -> rent_vs_buy -> score -> (hot alerts | weekly digest)

A stage failing for one listing must not sink the run: record it, skip that
listing, and surface the count in the digest footer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate, ChangeEvent


@dataclass
class RunResult:
    candidates: list[Candidate] = field(default_factory=list)
    rentals: list[Candidate] = field(default_factory=list)
    changes: list[ChangeEvent] = field(default_factory=list)
    outages: list[str] = field(default_factory=list)
    alerts_sent: int = 0


def scan(dry_run: bool = False) -> RunResult:
    """Daily: collect, enrich, score, persist, send hot alerts not yet sent."""
    raise NotImplementedError("WP9")


def digest(dry_run: bool = False) -> RunResult:
    """Weekly: scan, write this week's MarketSnapshot per area, send the digest."""
    raise NotImplementedError("WP9")


def analyze(url_or_address: str) -> Candidate:
    """On demand: full write-up of one property, printed to the terminal."""
    raise NotImplementedError("WP9")
