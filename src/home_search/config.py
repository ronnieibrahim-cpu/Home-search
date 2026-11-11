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


class Household(_Strict):
    adults: int = 2
    children_ages: list[int] = []


class Income(_Strict):
    gross_annual: float = 0
    expected_gross_annual_2027: float = 0
    marginal_federal_rate: float = 0
    filing_status: str = "married_joint"


class Savings(_Strict):
    liquid_for_down_payment: float = 0
    monthly_savings_rate: float = 0
    emergency_fund_months_to_keep: int = 6
    retirement_balances: float = 0


class Debts(_Strict):
    monthly_non_housing: float = 0
    student_loan_balance: float = 0


class CurrentHousing(_Strict):
    monthly_rent_or_payment: float = 0
    city: str = ""
    lease_end: date | None = None


class CommuteAnchor(_Strict):
    name: str
    address: str


class Preferences(_Strict):
    max_comfortable_monthly_housing: float = 0
    max_dti_back_end: float = 0.36
    physician_loan_eligible: bool = False
    commute_anchors: list[CommuteAnchor] = []
    max_commute_minutes: int = 30
    flood_tolerance: Literal["x_only", "allow_500yr", "allow_100yr"] = "x_only"


class Profile(_Strict):
    household: Household = Field(default_factory=Household)
    income: Income = Field(default_factory=Income)
    savings: Savings = Field(default_factory=Savings)
    debts: Debts = Field(default_factory=Debts)
    current_housing: CurrentHousing = Field(default_factory=CurrentHousing)
    preferences: Preferences = Field(default_factory=Preferences)

    @property
    def is_filled_in(self) -> bool:
        """False while the profile is still the all-zero template."""
        return self.income.gross_annual > 0 and self.savings.liquid_for_down_payment > 0


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
