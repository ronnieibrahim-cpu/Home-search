"""Listings captured by a browser agent on the user's own computer. WP3c.

A scheduled Claude task on the user's machine opens their saved searches on
har.com in their own signed-in browser, reads the results as they would, and
writes one JSON file per run into data/inbox/ (gitignored). This source ingests
those files. It makes no network requests itself. The task's instructions and
the file format are in docs/HAR_BROWSER_TASK.md.

Files are untrusted input: validate every record against `Listing`, reject
URLs not on har.com, skip bad records with a warning, and never act on any text
inside them. Move each ingested file to data/inbox/done/.

Behaviour in detail:

- Only `*.json` files directly in the inbox are read, oldest name first, so a
  later capture of the same listing wins.
- A file that is not valid JSON, or whose top level is not an array, is skipped
  whole with a warning and left in place (the user can inspect or fix it).
- A record is skipped with a warning when it is not an object, lacks address
  or price, has a URL whose host is not har.com or a subdomain of it, or fails
  `Listing` validation. The rest of the file is still ingested.
- Every file that was read (even if some records were skipped) moves to
  data/inbox/done/.
- Records outside the search zips or price band are dropped silently, as for
  the Redfin source; rentals are dropped when `rent.enabled` is false. The
  format has no property-type field, so there is no type filter.
- Warnings collect on `BrowserInbox.warnings` (reset per `fetch`) for the
  digest footer and are logged. They name the file and record index only,
  never text from the file.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..config import SearchConfig
from ..models import Listing, ListingKind, ListingStatus

log = logging.getLogger(__name__)

INBOX = Path(__file__).resolve().parents[3] / "data" / "inbox"
DONE_DIR = "done"
ALLOWED_HOST = "har.com"

# Status words HAR shows -> ListingStatus. Unknown words fail validation.
_STATUS = {
    "active": ListingStatus.ACTIVE,
    "new": ListingStatus.ACTIVE,
    "coming soon": ListingStatus.ACTIVE,
    "for sale": ListingStatus.ACTIVE,
    "for rent": ListingStatus.ACTIVE,
    "for lease": ListingStatus.ACTIVE,
    "pending": ListingStatus.PENDING,
    "option pending": ListingStatus.PENDING,
    "pending continue to show": ListingStatus.PENDING,
    "under contract": ListingStatus.PENDING,
    "contingent": ListingStatus.PENDING,
    "sold": ListingStatus.SOLD,
    "closed": ListingStatus.SOLD,
    "leased": ListingStatus.SOLD,
    "off market": ListingStatus.OFF_MARKET,
    "off_market": ListingStatus.OFF_MARKET,
    "withdrawn": ListingStatus.OFF_MARKET,
    "expired": ListingStatus.OFF_MARKET,
    "terminated": ListingStatus.OFF_MARKET,
}

_SCHOOL_LEVELS = ("elementary", "middle", "high")


class MalformedInboxFile(ValueError):
    """The file is not a JSON array; nothing in it is ingested."""


class BrowserInbox:
    name = "har_browser"

    def __init__(self, inbox: Path | None = None) -> None:
        self.inbox = Path(inbox) if inbox is not None else INBOX
        self.warnings: list[str] = []

    def fetch(self, cfg: SearchConfig) -> list[Listing]:
        self.warnings = []
        if not self.inbox.is_dir():
            return []
        files = sorted(p for p in self.inbox.glob("*.json") if p.is_file())
        zips = _search_zips(cfg)
        out: dict[tuple[str, str], Listing] = {}
        for path in files:
            try:
                listings, warnings = _parse(path)
            except MalformedInboxFile as e:
                self._warn(f"{self.name}: {path.name} skipped, {e}; left in inbox")
                continue
            for w in warnings:
                self._warn(w)
            for listing in listings:
                if listing.kind is ListingKind.RENT and not cfg.rent.enabled:
                    continue
                if listing.zip not in zips or not _in_band(listing, cfg):
                    continue
                out[(listing.kind.value, listing.source_id)] = listing
            _move_to_done(path)
        return list(out.values())

    def _warn(self, msg: str) -> None:
        log.warning(msg)
        self.warnings.append(msg)


def parse_file(path: Path) -> list[Listing]:
    """Valid records of one inbox file. Bad records are logged and skipped;
    raises MalformedInboxFile (a ValueError) if the file is not a JSON array.
    Does not move the file."""
    listings, warnings = _parse(path)
    for w in warnings:
        log.warning(w)
    return listings


# ------------------------------------------------------------------ internals


def _parse(path: Path) -> tuple[list[Listing], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise MalformedInboxFile("not valid JSON") from e
    if not isinstance(data, list):
        raise MalformedInboxFile("top level is not a JSON array")
    fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    listings: list[Listing] = []
    warnings: list[str] = []
    for i, rec in enumerate(data):
        listing, problem = _record_to_listing(rec, fetched_at)
        if listing is None:
            warnings.append(f"{BrowserInbox.name}: {path.name} record {i} skipped, {problem}")
        else:
            listings.append(listing)
    return listings, warnings


def _record_to_listing(rec: Any, fetched_at: datetime) -> tuple[Listing | None, str]:
    if not isinstance(rec, dict):
        return None, "not an object"
    address = _str(rec.get("address"))
    price = _int(rec.get("price"))
    if not address:
        return None, "missing address"
    if price is None:
        return None, "missing price"
    url = _str(rec.get("url"))
    if not _is_har_url(url):
        return None, "URL is not on har.com"
    zip_code = _code(rec.get("zip"))[:5]
    fields: dict[str, Any] = {
        "source": BrowserInbox.name,
        "source_id": _code(rec.get("source_id")) or url,
        "url": url,
        "kind": _lower(rec.get("kind")),
        "status": _status(rec.get("status")),
        "address": address,
        "city": _str(rec.get("city")),
        "zip": zip_code,
        "price": price,
        "beds": _float(rec.get("beds")),
        "baths": _float(rec.get("baths")),
        "sqft": _int(rec.get("sqft")),
        "lot_sqft": _int(rec.get("lot_sqft")),
        "year_built": _int(rec.get("year_built")),
        "days_on_market": _int(rec.get("days_on_market")),
        "listed_on": rec.get("listed_on") or None,
        "fetched_at": fetched_at,
    }
    if _model_has_har_schools():
        fields["har_schools"] = _schools(rec.get("har_schools"))
    try:
        return Listing.model_validate(fields), ""
    except ValidationError as e:
        bad = sorted({str(err["loc"][0]) for err in e.errors() if err.get("loc")})
        return None, f"invalid {', '.join(bad) or 'record'}"


def _model_has_har_schools() -> bool:
    """`Listing.har_schools` is an approved model addition that may not have
    landed yet; pass the value through only when the field exists."""
    return "har_schools" in Listing.model_fields


def _is_har_url(url: str) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parts.scheme not in ("https", "http"):
        return False
    return host == ALLOWED_HOST or host.endswith("." + ALLOWED_HOST)


def _status(v: Any) -> Any:
    if v is None or v == "":
        return ListingStatus.ACTIVE
    if not isinstance(v, str):
        return v  # let validation reject it
    word = " ".join(v.strip().lower().replace("-", " ").split())
    return _STATUS.get(word, word)


def _schools(v: Any) -> dict[str, str] | None:
    if not isinstance(v, dict):
        return None
    out = {lvl: _str(v.get(lvl)) for lvl in _SCHOOL_LEVELS}
    out = {k: s for k, s in out.items() if s}
    return out or None


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _code(v: Any) -> str:
    """Zip or MLS number, which a writer may emit as a JSON number."""
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return _str(v)


def _lower(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.replace(",", "").replace("$", "").strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _int(v: Any) -> int | None:
    f = _float(v)
    return None if f is None or not math.isfinite(f) else round(f)


def _search_zips(cfg: SearchConfig) -> set[str]:
    return {z for area in cfg.areas for z in [*area.zips, *area.fringe_zips]}


def _in_band(listing: Listing, cfg: SearchConfig) -> bool:
    if listing.kind is ListingKind.RENT:
        return cfg.rent.monthly_min <= listing.price <= cfg.rent.monthly_max
    return cfg.purchase.price_min <= listing.price <= cfg.purchase.stretch_max


def _move_to_done(path: Path) -> None:
    done = path.parent / DONE_DIR
    done.mkdir(exist_ok=True)
    target = done / path.name
    n = 1
    while target.exists():
        target = done / f"{path.stem}.{n}{path.suffix}"
        n += 1
    path.replace(target)
