"""Monthly cost of owning a specific listing under each loan scenario. WP7.

Pure functions: no I/O, no profile. Spec and worked example in docs/FINANCE_MODEL.md.
"""

from __future__ import annotations

from ..config import Assumptions, LoanScenario
from ..models import FloodRisk, Listing, LoanCost


def monthly_payment(principal: float, annual_rate: float, years: int) -> float:
    """Standard fixed-rate amortization payment. Rate 0 must not divide by zero."""
    raise NotImplementedError("WP7")


def loan_cost(
    listing: Listing, area_id: str, flood: FloodRisk, scenario: LoanScenario, a: Assumptions
) -> LoanCost:
    raise NotImplementedError("WP7")


def all_scenarios(
    listing: Listing, area_id: str, flood: FloodRisk, a: Assumptions
) -> list[LoanCost]:
    raise NotImplementedError("WP7")
