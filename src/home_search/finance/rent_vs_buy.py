"""Rent-vs-buy verdict per listing and for the search as a whole. WP8.

Method (full spec in docs/FINANCE_MODEL.md): simulate two households with the
same income and starting cash over the horizon. The buyer pays the down payment
and ownership costs and ends with home equity net of selling costs. The renter
pays rent on a comparable home and invests the down payment plus, each month,
whatever the buyer spends beyond rent (or vice versa). Compare terminal net
worth, discounted to today.
"""

from __future__ import annotations

from ..config import Assumptions, Profile
from ..models import Listing, LoanCost, RentVsBuy


def rent_equivalent(listing: Listing, rentals: list[Listing], a: Assumptions) -> tuple[float, str]:
    """Monthly rent for a comparable home, and where it came from.

    Prefer the median of active rentals in the same high-school zone within
    +/-20% sqft and +/-1 bed; fall back to price / fallback_price_to_annual_rent.
    """
    raise NotImplementedError("WP8")


def analyze(
    listing: Listing,
    cost: LoanCost,
    rentals: list[Listing],
    profile: Profile | None,
    a: Assumptions,
) -> RentVsBuy:
    raise NotImplementedError("WP8")


def portfolio_view(results: list[RentVsBuy]) -> dict:
    """The digest's headline: share of candidates where buying wins, median
    break-even year, and the single assumption the verdict is most sensitive to."""
    raise NotImplementedError("WP8")
