"""Can this household carry this listing, and what blocks it if not. WP7.

Reads the private profile. Outputs go only into the emailed digest.
"""

from __future__ import annotations

from datetime import date

from ..config import Assumptions, Profile
from ..models import Affordability, LoanCost


def cash_at(profile: Profile, when: date, a: Assumptions) -> float:
    """Liquid savings projected to the purchase date, net of the emergency fund kept."""
    raise NotImplementedError("WP7")


def assess(cost: LoanCost, price: int, profile: Profile, a: Assumptions, when: date) -> Affordability:
    raise NotImplementedError("WP7")


def max_price(profile: Profile, area_id: str, a: Assumptions, when: date) -> dict[str, int]:
    """Highest price that passes `assess` under each loan scenario. Shown at the
    top of every digest so the budget band is tested against the real numbers."""
    raise NotImplementedError("WP7")
