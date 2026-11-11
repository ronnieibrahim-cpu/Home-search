"""Build the digest and hot-alert emails (HTML + plain text). WP10.

Section order for the weekly digest:
  1. Advisor summary: rent-vs-buy headline, max affordable price, savings runway to 2027
  2. Top candidates (ranked, with score breakdown, monthly cost, zoning, flood)
  3. What changed: new, price cuts, went pending
  4. Rentals worth a look
  5. Market trend per area (from MarketSnapshot history)
  6. Zoning-unverified listings (manual check needed)
  7. Footer: source outages, assumptions as_of date, flood and zoning caveats
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Candidate, ChangeEvent, MarketSnapshot


@dataclass
class Email:
    subject: str
    html: str
    text: str


def weekly(candidates: list[Candidate], rentals: list[Candidate], changes: list[ChangeEvent],
           market: list[MarketSnapshot], advisor: dict, outages: list[str]) -> Email:
    raise NotImplementedError("WP10")


def hot_alert(candidate: Candidate) -> Email:
    raise NotImplementedError("WP10")
