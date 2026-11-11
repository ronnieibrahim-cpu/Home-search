"""Redfin search-results CSV download, per zip. WP3.

Uses the same CSV export a signed-out browser gets from a search page. Rules:
one request per zip per run, a 5-10 s jittered pause between requests, an honest
User-Agent, no retries on 403/429 (raise SourceUnavailable), and no detail-page
fetching. If Redfin blocks the runner, the email source still covers the run.

"One request per zip per run" is per kind: a zip is fetched once for sale
listings and, when `rent.enabled`, once for rentals. Any refusal (non-200, an
HTML page or challenge instead of CSV, an unrecognised header) stops the run for
this source at once; no further requests are made. A well-formed CSV with a
header and no rows is a genuine "nothing listed" and yields `[]` for that zip.
"""

from __future__ import annotations

import csv
import io
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from ..config import SearchConfig
from ..models import Listing, ListingKind, ListingStatus
from .base import SourceUnavailable

log = logging.getLogger(__name__)

CSV_URL = "https://www.redfin.com/stingray/api/gis-csv"
USER_AGENT = (
    "home-search/0.1 (personal, non-commercial home-search script; "
    "one request per zip per day)"
)
PAUSE_RANGE_S = (5.0, 10.0)
TIMEOUT_S = 30.0

# Redfin "PROPERTY TYPE" values -> the names used in search.yaml home.property_types.
PROPERTY_TYPES = {
    "single family residential": "single_family",
    "townhouse": "townhouse",
    "condo/co-op": "condo",
    "multi-family (2-4 unit)": "multi_family",
    "multi-family (5+ unit)": "multi_family",
    "vacant land": "land",
    "mobile/manufactured home": "mobile",
    "ranch": "ranch",
    "other": "other",
}

# Redfin "uipt" filter codes for the property types above (sent so the server
# returns less; the local filter in `fetch` is still authoritative).
_UIPT = {"single_family": "1", "condo": "2", "townhouse": "3", "multi_family": "4", "land": "5"}

_STATUS = {
    "active": ListingStatus.ACTIVE,
    "coming soon": ListingStatus.ACTIVE,
    "for rent": ListingStatus.ACTIVE,
    "pending": ListingStatus.PENDING,
    "contingent": ListingStatus.PENDING,
    "under contract": ListingStatus.PENDING,
    "sold": ListingStatus.SOLD,
    "off market": ListingStatus.OFF_MARKET,
}

_REQUIRED = ("ADDRESS", "ZIP OR POSTAL CODE", "PRICE")
_HOME_ID = re.compile(r"/home/(\d+)")


class RedfinCsv:
    name = "redfin_csv"

    def __init__(
        self,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._rng = rng or random.Random()

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        zips = _search_zips(cfg)
        kinds = [ListingKind.SALE] + ([ListingKind.RENT] if cfg.rent.enabled else [])
        wanted_types = set(cfg.home.property_types)
        client = self._client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/csv"},
            timeout=TIMEOUT_S,
            follow_redirects=False,
        )
        out: dict[tuple[str, str], Listing] = {}
        first = True
        try:
            for kind in kinds:
                for z in zips:
                    if not first:
                        self._sleep(self._rng.uniform(*PAUSE_RANGE_S))
                    first = False
                    text = _get(client, build_url(z, kind, cfg.home.property_types))
                    rows = _parse(text, kind)
                    if not rows:
                        log.info("redfin_csv: zip %s returned no %s listings", z, kind.value)
                    for listing, ptype in rows:
                        if listing.zip not in zips:
                            continue
                        if ptype not in wanted_types:
                            continue
                        if not _in_band(listing, cfg):
                            continue
                        out[(listing.kind.value, listing.source_id)] = listing
        finally:
            if self._client is None:
                client.close()
        return list(out.values())


def build_url(zip_code: str, kind: ListingKind | str, property_types: list[str]) -> str:
    """CSV export URL for one zip. Parameter names mirror the site's own
    "Download All" link; region_type 2 is Redfin's zip-code region type."""
    uipt = ",".join(sorted({_UIPT[t] for t in property_types if t in _UIPT})) or "1"
    params = {
        "al": "1",
        "num_homes": "350",
        "ord": "redfin-recommended-asc",
        "page_number": "1",
        "region_id": zip_code,
        "region_type": "2",
        "status": "9",
        "uipt": uipt,
        "v": "8",
    }
    if ListingKind(kind) is ListingKind.RENT:
        params["isRentals"] = "true"
    return str(httpx.URL(CSV_URL, params=params))


def parse_csv(text: str, kind: str) -> list[Listing]:
    """Pure parser, separated so it is tested against tests/fixtures without network."""
    return [listing for listing, _ in _parse(text, ListingKind(kind))]


# ------------------------------------------------------------------ internals


def _search_zips(cfg: SearchConfig) -> list[str]:
    seen: list[str] = []
    for area in cfg.areas:
        for z in [*area.zips, *area.fringe_zips]:
            if z not in seen:
                seen.append(z)
    return seen


def _in_band(listing: Listing, cfg: SearchConfig) -> bool:
    if listing.kind is ListingKind.RENT:
        return cfg.rent.monthly_min <= listing.price <= cfg.rent.monthly_max
    return cfg.purchase.price_min <= listing.price <= cfg.purchase.stretch_max


def _get(client: httpx.Client, url: str) -> str:
    """Exactly one request. No retries, whatever the outcome."""
    try:
        resp = client.get(url)
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"redfin_csv: request failed ({type(e).__name__})") from e
    if resp.status_code in (403, 429):
        raise SourceUnavailable(f"redfin_csv: blocked (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise SourceUnavailable(f"redfin_csv: unexpected HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "").lower()
    if "html" in ctype:
        raise SourceUnavailable("redfin_csv: got an HTML page instead of CSV")
    return resp.text


def _parse(text: str, kind: ListingKind) -> list[tuple[Listing, str]]:
    body = text.lstrip("﻿").strip()
    if not body:
        raise SourceUnavailable("redfin_csv: empty response")
    if body.startswith("<") or body[:200].lower().find("<html") >= 0:
        raise SourceUnavailable("redfin_csv: got an HTML page instead of CSV")
    reader = csv.DictReader(io.StringIO(body))
    header = [h.strip().upper() for h in (reader.fieldnames or [])]
    missing = [c for c in _REQUIRED if c not in header]
    if missing:
        raise SourceUnavailable(f"redfin_csv: CSV format changed, missing {missing}")
    url_col = next((h for h in (reader.fieldnames or []) if h.strip().upper().startswith("URL")),
                   None)
    fetched_at = datetime.now(UTC)
    out: list[tuple[Listing, str]] = []
    for raw in reader:
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in raw.items()
               if isinstance(v, str) or v is None}
        listing = _row_to_listing(row, raw.get(url_col, "") if url_col else "", kind,
                                  fetched_at)
        if listing is None:
            continue
        ptype = PROPERTY_TYPES.get(row.get("PROPERTY TYPE", "").lower(), "other")
        out.append((listing, ptype))
    return out


def _row_to_listing(row: dict[str, str], url: str, kind: ListingKind,
                    fetched_at: datetime) -> Listing | None:
    address = row.get("ADDRESS", "")
    price = _int(row.get("PRICE"))
    zip_code = row.get("ZIP OR POSTAL CODE", "")[:5]
    # Skips Redfin's disclaimer row ("In accordance with local MLS rules ...")
    # and past-sale rows, which carry no current asking price.
    if not address or price is None or not zip_code:
        return None
    if row.get("SALE TYPE", "").upper() == "PAST SALE":
        return None
    url = (url or "").strip()
    m = _HOME_ID.search(url)
    source_id = m.group(1) if m else (row.get("MLS#") or f"{address}|{zip_code}")
    return Listing(
        source=RedfinCsv.name,
        source_id=source_id,
        url=url,
        kind=kind,
        status=_STATUS.get(row.get("STATUS", "").lower(), ListingStatus.ACTIVE),
        address=address,
        city=row.get("CITY", ""),
        zip=zip_code,
        lat=_float(row.get("LATITUDE")),
        lon=_float(row.get("LONGITUDE")),
        price=price,
        beds=_float(row.get("BEDS")),
        baths=_float(row.get("BATHS")),
        sqft=_int(row.get("SQUARE FEET")),
        lot_sqft=_int(row.get("LOT SIZE")),
        year_built=_int(row.get("YEAR BUILT")),
        hoa_monthly=_int(row.get("HOA/MONTH")),
        days_on_market=_int(row.get("DAYS ON MARKET")),
        fetched_at=fetched_at,
    )


def _float(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.replace(",", "").replace("$", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v: str | None) -> int | None:
    f = _float(v)
    return None if f is None else round(f)
