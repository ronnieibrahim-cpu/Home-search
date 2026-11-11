"""Domain types passed between pipeline stages. Stages add fields; none mutate upstream ones."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel


class ListingKind(StrEnum):
    SALE = "sale"
    RENT = "rent"


class ListingStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    SOLD = "sold"
    OFF_MARKET = "off_market"


class Listing(BaseModel):
    """One property as reported by one source. `key` dedupes across sources."""

    source: str                       # "redfin_csv" | "email_har" | "email_redfin" | ...
    source_id: str
    url: str
    kind: ListingKind
    status: ListingStatus = ListingStatus.ACTIVE
    address: str
    city: str
    zip: str
    lat: float | None = None
    lon: float | None = None
    price: int                        # list price, or monthly rent when kind == RENT
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    lot_sqft: int | None = None
    year_built: int | None = None
    hoa_monthly: int | None = None
    days_on_market: int | None = None
    listed_on: date | None = None
    description: str | None = None
    fetched_at: datetime

    @property
    def key(self) -> str:
        """Normalized address + zip + kind. Implemented in WP2 (store.normalize_key)."""
        from .store import normalize_key

        return normalize_key(self.address, self.zip, self.kind)


class ZoningConfidence(StrEnum):
    VERIFIED = "verified"             # point-in-polygon against the district boundary file
    LISTING_CLAIM = "listing_claim"   # only the listing text says so
    UNKNOWN = "unknown"


class SchoolZoning(BaseModel):
    district: str | None = None
    elementary: str | None = None
    middle: str | None = None
    high: str | None = None
    confidence: ZoningConfidence = ZoningConfidence.UNKNOWN
    ratings: dict[str, int] = {}      # school name -> 1-10
    boundary_vintage: str | None = None   # school year of the boundary file used


class FloodRisk(BaseModel):
    fema_zone: str | None = None      # "X", "X_shaded", "AE", "floodway"
    in_floodway: bool = False
    notes: list[str] = []             # e.g. reservoir pool, description mentions prior flooding


class LoanCost(BaseModel):
    scenario_id: str
    down_payment: float
    loan_amount: float
    rate: float
    is_jumbo: bool
    principal_interest: float         # monthly, as are the rest
    property_tax: float
    homeowners_insurance: float
    flood_insurance: float
    pmi: float
    hoa: float
    maintenance: float

    @property
    def total_monthly(self) -> float:
        return (
            self.principal_interest + self.property_tax + self.homeowners_insurance
            + self.flood_insurance + self.pmi + self.hoa + self.maintenance
        )


class Affordability(BaseModel):
    cash_needed: float                # down payment + closing + reserves kept
    cash_available_at_purchase: float
    dti_back_end: float
    within_comfort: bool
    blockers: list[str] = []          # human-readable, shown in the digest


class RentVsBuy(BaseModel):
    horizon_years: int
    rent_equivalent_monthly: float
    rent_source: str                  # "comp:<listing key>" or "fallback_ratio"
    npv_buy_minus_rent: float         # > 0 means buying leaves you wealthier at horizon
    breakeven_year: int | None        # None = renting wins at every horizon tested
    verdict: str                      # "buy" | "rent" | "close_call"
    rationale: list[str]              # 2-4 plain-language sentences for the email
    sensitivity: dict[str, float] = {}    # "h=10,app=0.02,rate=+0.01" -> npv


class Candidate(BaseModel):
    """A listing after every stage. This is what the digest renders."""

    listing: Listing
    zoning: SchoolZoning
    flood: FloodRisk
    costs: list[LoanCost] = []
    affordability: Affordability | None = None    # None when no profile is loaded
    rent_vs_buy: RentVsBuy | None = None
    score: float = 0.0
    score_parts: dict[str, float] = {}
    flags: list[str] = []             # "stretch", "price_cut", "zoning_unverified", ...


class ChangeEvent(BaseModel):
    """What changed since the last run; the digest's "what's new" section."""

    key: str
    kind: str                         # "new" | "price_change" | "status_change" | "removed"
    old: str | None = None
    new: str | None = None
    at: datetime


class MarketSnapshot(BaseModel):
    """Weekly per-area stats, so the 2027 decision has a trend line behind it."""

    area_id: str
    week_of: date
    active_count: int
    median_price: int | None
    median_ppsf: float | None
    median_dom: int | None
    median_rent: int | None
    price_cut_share: float | None
