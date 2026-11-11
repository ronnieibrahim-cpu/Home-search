"""Typed loading of the three config files.

search.yaml and assumptions.yaml are public and live in the repo. The profile
is private: it comes from config/profile.yaml locally or the PROFILE_YAML
secret in CI, and must never be logged or written to a committed path.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- search.yaml


class Purchase(_Strict):
    target_year: int
    price_min: int
    price_max: int
    stretch_pct: float = 0.0

    @property
    def stretch_max(self) -> int:
        return int(self.price_max * (1 + self.stretch_pct))


class HomeCriteria(_Strict):
    beds_min: int = 0
    baths_min: float = 0
    sqft_min: int = 0
    lot_sqft_min: int = 0
    year_built_min: int = 0
    property_types: list[str] = ["single_family"]
    exclude_keywords: list[str] = []


class RentCriteria(_Strict):
    enabled: bool = True
    monthly_min: int = 0
    monthly_max: int = 0


class TargetSchools(_Strict):
    high: list[str]
    preferred_high: str | None = None
    middle: list[str] = []
    elementary: list[str] = []


class Area(_Strict):
    id: str
    name: str
    district: str
    zips: list[str]
    fringe_zips: list[str] = []
    target_schools: TargetSchools


class SchoolRules(_Strict):
    require_verified_high: bool = True
    min_rating: int = 0


class Scoring(_Strict):
    weights: dict[str, float]
    hot_alert_threshold: int = 85
    digest_min_score: int = 60
    digest_max_items: int = 15

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Scoring:
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"scoring.weights must sum to 1.0, got {total}")
        return self


class Schedule(_Strict):
    timezone: str = "America/Chicago"
    scan: Literal["daily", "twice_daily"] = "daily"
    digest_day: str = "sunday"


class SearchConfig(_Strict):
    purchase: Purchase
    home: HomeCriteria
    rent: RentCriteria
    areas: list[Area]
    schools: SchoolRules
    scoring: Scoring
    schedule: Schedule

    def area(self, area_id: str) -> Area:
        return next(a for a in self.areas if a.id == area_id)


# ----------------------------------------------------------- assumptions.yaml


class LoanScenario(_Strict):
    id: str
    down_pct: float
    pmi: bool = False
    rate_spread: float = 0.0


class Mortgage(_Strict):
    rate_30yr_fixed: float
    jumbo_rate_spread: float = 0.0
    conforming_limit: int
    term_years: int = 30
    scenarios: list[LoanScenario]
    pmi_annual_pct: float = 0.005


class PropertyTax(_Strict):
    rate_by_area: dict[str, float]
    # School district's fraction of the total rate per area; the homestead
    # exemption reduces only this share (docs/FINANCE_MODEL.md §1).
    school_share_by_area: dict[str, float]
    homestead_exemption_school: int = 0
    homestead_cap_annual: float = 0.10


class Insurance(_Strict):
    homeowners_pct_of_value: float
    flood_annual_by_zone: dict[str, int | None]


class Ownership(_Strict):
    maintenance_pct_of_value: float
    closing_cost_buy_pct: float
    closing_cost_sell_pct: float
    home_appreciation: float
    hoa_monthly_default: int = 0


class Renting(_Strict):
    rent_growth: float
    renters_insurance_annual: int = 0
    fallback_price_to_annual_rent: float


class Investing(_Strict):
    return_nominal: float
    inflation: float


class Tax(_Strict):
    mortgage_interest_debt_cap: int
    salt_cap: int


class Sensitivity(_Strict):
    horizons: list[int]
    appreciation: list[float]
    rate: list[float]


class Assumptions(_Strict):
    as_of: date | None = None
    mortgage: Mortgage
    property_tax: PropertyTax
    insurance: Insurance
    ownership: Ownership
    renting: Renting
    investing: Investing
    tax: Tax
    horizon_years: int
    sensitivity: Sensitivity


# --------------------------------------------------------------- profile.yaml
# Shape follows the household's own planning export (home_search_budget.yaml)
# so that file can be dropped in unchanged. Unknown keys are ignored; derived
# tables in it (price_grid, projected balances) are recomputed, never trusted.


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProfileMeta(_Loose):
    generated: date | None = None            # balances are as of this date
    target_close_date: date | None = None    # default: June 1 of purchase.target_year


class ProfileSearch(_Loose):
    price_min: int | None = None             # when set, overrides search.yaml
    price_max: int | None = None
    planning_price: int | None = None
    bedrooms_min: int | None = None
    bathrooms_min: float | None = None


class Income(_Loose):
    gross_monthly_for_lender: float = 0      # what DTI is measured against
    net_take_home_monthly_post_home: float = 0


class CashForPurchase(_Loose):
    balance: float = 0
    monthly_addition: float = 0
    keep_after_closing: float = 0            # emergency buffer; never spent on the house


class Financing(_Loose):
    down_payment_pct: float = 0.20
    closing_costs_pct: float | None = None   # overrides ownership.closing_cost_buy_pct
    rate_assumption: float | None = None     # overrides mortgage.rate_30yr_fixed
    rate_range: list[float] = []
    physician_loan_programs: str | None = None   # "consider" enables that scenario


class CarryingCosts(_Loose):
    property_tax_rate_modeled: float | None = None   # used when a listing has no actual rate
    insurance_plus_maintenance_annual: float | None = None   # the plan's combined figure
    total_housing_carry_target_monthly: list[float] = []     # [comfortable, flag-above]
    hoa_monthly: float | None = None


class MonthlyBudget(_Loose):
    non_housing_living_costs: float = 0
    savings_and_investing: float = 0
    hard_ceiling_total_housing: float | None = None


class CommuteAnchor(_Loose):
    name: str
    address: str


class Household(_Loose):
    """Not in the planning export; optional extras the app can use."""

    children_ages: list[int] = []
    marginal_federal_rate: float | None = None
    monthly_non_housing_debt: float = 0
    current_rent_monthly: float | None = None
    flood_tolerance: Literal["x_only", "allow_500yr", "allow_100yr"] = "x_only"
    commute_anchors: list[CommuteAnchor] = []
    max_commute_minutes: int = 30


class Profile(_Loose):
    meta: ProfileMeta = Field(default_factory=ProfileMeta)
    search: ProfileSearch = Field(default_factory=ProfileSearch)
    income: Income = Field(default_factory=Income)
    cash_for_purchase: CashForPurchase = Field(default_factory=CashForPurchase)
    other_assets: dict[str, float] = {}      # lender reserves; never down payment
    financing: Financing = Field(default_factory=Financing)
    carrying_costs: CarryingCosts = Field(default_factory=CarryingCosts)
    monthly_budget_after_purchase: MonthlyBudget = Field(default_factory=MonthlyBudget)
    household: Household = Field(default_factory=Household)

    @property
    def is_filled_in(self) -> bool:
        """False while the profile is still the all-zero template."""
        return self.income.gross_monthly_for_lender > 0 and self.cash_for_purchase.balance > 0

    @property
    def carry_flag_above(self) -> float | None:
        t = self.carrying_costs.total_housing_carry_target_monthly
        return max(t) if t else None


# -------------------------------------------------------------------- loaders


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def load_search(path: Path | None = None) -> SearchConfig:
    return SearchConfig.model_validate(_read(path or CONFIG_DIR / "search.yaml"))


def load_assumptions(path: Path | None = None) -> Assumptions:
    return Assumptions.model_validate(_read(path or CONFIG_DIR / "assumptions.yaml"))


def load_profile(path: Path | None = None) -> Profile | None:
    """PROFILE_YAML env wins (CI), then config/profile.yaml. None if neither exists.

    Callers must degrade gracefully on None: listings are still collected and
    scored, but affordability and the rent-vs-buy verdict are omitted.
    """
    raw = os.environ.get("PROFILE_YAML")
    if raw:
        return Profile.model_validate(yaml.safe_load(raw) or {})
    path = path or CONFIG_DIR / "profile.yaml"
    if path.exists():
        return Profile.model_validate(_read(path))
    return None
