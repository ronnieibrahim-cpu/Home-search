"""SQLite state: listings seen, their history, sent alerts, market snapshots. WP2.

The database holds listing data only. Nothing from the profile is ever stored.
Locally it lives at data/state.sqlite (gitignored); in CI the workflow restores
it from, and pushes it back to, the orphan `state` branch.

Tables
------
listings         one row per key: the merged current state (Listing JSON in `data`)
                 plus `field_sources`, which source supplied each field.
listing_sources  (key, source, url): every URL any source reported for a key, with
                 that source's consecutive miss count for `mark_missing`.
history          (key, kind, field, old, new, at): change events, read by
                 `changes_since`.
alerts           (key, price, at): hot alerts already sent.
snapshots        one row per (area_id, week_of).
meta             small key/value store (e.g. IMAP UIDs already processed).

Merge rule: a field reported by a HAR source (name starts with "har" or is
"email_har") wins over the same field from any other source, in the same run and
across runs, except that price, status and days on market always take the
newest run's value. Otherwise a non-null value wins over null, and among records of
equal rank the one with more non-null fields, then the most recently fetched,
wins.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self

from .models import ChangeEvent, Listing, ListingKind, ListingStatus, MarketSnapshot

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "state.sqlite"

SCHEMA_VERSION = 1
MISSES_BEFORE_OFF_MARKET = 2

# Fields that identify which record a listing row came from; they move together.
_IDENTITY_FIELDS = ("source", "source_id", "url")
# Fields where the most recent report wins across runs, regardless of source.
_VOLATILE_FIELDS = ("price", "status", "days_on_market")
# Statuses mark_missing leaves alone: the listing is already gone.
_CLOSED_STATUSES = (ListingStatus.OFF_MARKET.value, ListingStatus.SOLD.value)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    price         INTEGER NOT NULL,
    zip           TEXT NOT NULL,
    data          TEXT NOT NULL,
    field_sources TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listings_kind_status ON listings (kind, status);

CREATE TABLE IF NOT EXISTS listing_sources (
    key        TEXT NOT NULL,
    source     TEXT NOT NULL,
    url        TEXT NOT NULL,
    source_id  TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    misses     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, source, url)
);
CREATE INDEX IF NOT EXISTS listing_sources_source ON listing_sources (source);

CREATE TABLE IF NOT EXISTS history (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    key   TEXT NOT NULL,
    kind  TEXT NOT NULL,
    field TEXT NOT NULL,
    old   TEXT,
    new   TEXT,
    at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_at ON history (at);

CREATE TABLE IF NOT EXISTS alerts (
    key   TEXT NOT NULL,
    price INTEGER NOT NULL,
    at    TEXT NOT NULL,
    PRIMARY KEY (key, price)
);

CREATE TABLE IF NOT EXISTS snapshots (
    area_id         TEXT NOT NULL,
    week_of         TEXT NOT NULL,
    active_count    INTEGER NOT NULL,
    median_price    INTEGER,
    median_ppsf     REAL,
    median_dom      INTEGER,
    median_rent     INTEGER,
    price_cut_share REAL,
    PRIMARY KEY (area_id, week_of)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# --- address normalization ---------------------------------------------------

_DIRECTIONS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}

_SUFFIXES = {
    "street": "st", "str": "st",
    "avenue": "ave", "av": "ave", "avn": "ave",
    "road": "rd",
    "drive": "dr", "drv": "dr",
    "lane": "ln",
    "court": "ct",
    "circle": "cir", "circ": "cir",
    "boulevard": "blvd", "boul": "blvd",
    "place": "pl",
    "terrace": "ter", "terr": "ter",
    "parkway": "pkwy", "pky": "pkwy",
    "highway": "hwy",
    "freeway": "fwy",
    "expressway": "expy",
    "trail": "trl",
    "square": "sq",
    "plaza": "plz",
    "point": "pt",
    "crossing": "xing",
    "cove": "cv",
    "hollow": "holw",
    "meadow": "mdw", "meadows": "mdws",
    "ridge": "rdg",
    "grove": "grv",
    "glen": "gln",
    "bend": "bnd",
    "creek": "crk",
}

_UNIT_WORDS = {"unit", "apt", "apartment", "ste", "suite", "bldg", "building"}
_STATE_WORDS = {"tx", "texas"}


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("#", " # ")
    text = re.sub(r"[^\w\s#-]", " ", text)
    return [t for t in re.split(r"[\s-]+", text) if t]


def _normalize_address(address: str) -> str:
    # A comma usually starts ", Houston, TX 77401"; keep only parts that carry a unit.
    parts = [p for p in address.split(",") if p.strip()]
    head, rest = (parts[0] if parts else ""), parts[1:]
    tokens = _tokens(head)
    for part in rest:
        part_tokens = _tokens(part)
        if part_tokens and (part_tokens[0] in _UNIT_WORDS or part_tokens[0] == "#"):
            tokens += part_tokens

    out: list[str] = []
    unit: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _UNIT_WORDS or tok == "#":
            # Everything after a unit marker is the unit id ("unit #a" -> "a").
            unit = [t for t in tokens[i + 1:] if t not in _UNIT_WORDS and t != "#"]
            break
        out.append(_DIRECTIONS.get(tok) or _SUFFIXES.get(tok) or tok)
        i += 1

    # Trailing state / zip tokens that slipped in without a comma.
    while out and (out[-1] in _STATE_WORDS or re.fullmatch(r"\d{5}(\d{4})?", out[-1])):
        if len(out) == 1:
            break
        out.pop()

    result = " ".join(out)
    if unit:
        result += " unit " + "".join(unit)
    return result


def normalize_key(address: str, zip_code: str, kind: ListingKind) -> str:
    """Stable cross-source identity for a property.

    Must collapse "123 N Main St", "123 North Main Street" and "123 N. MAIN ST
    #A" vs "Unit A" to one key, and must keep sale and rent listings apart.
    """
    zip5 = re.sub(r"\D", "", zip_code or "")[:5]
    return f"{ListingKind(kind).value}|{zip5}|{_normalize_address(address)}"


# --- helpers -----------------------------------------------------------------


def _is_har(source: str) -> bool:
    return source.startswith("har") or source == "email_har"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _non_null_count(listing: Listing) -> int:
    return sum(1 for v in listing.model_dump().values() if v is not None)


def _rank(listing: Listing) -> tuple[bool, int, datetime]:
    return (_is_har(listing.source), _non_null_count(listing), _aware(listing.fetched_at))


def _merge_group(records: list[Listing]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge one run's records for a key: field values and the source of each."""
    ordered = sorted(records, key=_rank, reverse=True)
    dumps = [(r.source, r.model_dump(mode="json")) for r in ordered]
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    top_source, top = dumps[0]
    for f in _IDENTITY_FIELDS:
        values[f] = top[f]
        sources[f] = top_source
    for f in Listing.model_fields:
        if f in _IDENTITY_FIELDS or f == "fetched_at":
            continue
        for source, d in dumps:
            if d[f] is not None:
                values[f], sources[f] = d[f], source
                break
        else:
            values[f], sources[f] = None, top_source
    values["fetched_at"] = max(_aware(r.fetched_at) for r in records).isoformat()
    return values, sources


def _combine(
    stored: dict[str, Any], stored_sources: dict[str, str],
    incoming: dict[str, Any], incoming_sources: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply a run's merged values onto the stored row, keeping HAR-sourced fields."""
    values = dict(stored)
    sources = dict(stored_sources)

    def takes_over(field: str) -> bool:
        # Price, status and days on market change over time, so the newest report
        # wins whatever its source; otherwise a stale HAR value would hide a
        # price cut or a pending sale that another source saw first.
        if field in _VOLATILE_FIELDS:
            return True
        # A non-HAR value never replaces a value HAR supplied; it may fill a HAR gap.
        har_held = _is_har(stored_sources.get(field, "")) and stored.get(field) is not None
        return not (har_held and not _is_har(incoming_sources[field]))

    if takes_over("source"):
        for f in _IDENTITY_FIELDS:
            values[f], sources[f] = incoming[f], incoming_sources[f]
    for f in Listing.model_fields:
        if f in _IDENTITY_FIELDS or f == "fetched_at":
            continue
        if incoming[f] is not None and takes_over(f):
            values[f], sources[f] = incoming[f], incoming_sources[f]
    values["fetched_at"] = max(
        _aware(datetime.fromisoformat(stored["fetched_at"])),
        _aware(datetime.fromisoformat(incoming["fetched_at"])),
    ).isoformat()
    return values, sources


# --- store -------------------------------------------------------------------


class Store:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None
        self._migrated = False

    # connection management

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path)
        if not self._migrated:
            self._migrated = True
            self.migrate()
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
            self._migrated = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def migrate(self) -> None:
        """Create or upgrade the schema. Idempotent."""
        self._migrated = True
        conn = self._conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        with conn:
            conn.executescript(_SCHEMA)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # listings

    def upsert(self, listings: list[Listing]) -> list[ChangeEvent]:
        """Merge a run's listings; return what is new or changed since last run.

        When two sources report the same key, prefer the record with more
        non-null fields but keep every source URL.
        """
        groups: dict[str, list[Listing]] = {}
        for listing in listings:
            groups.setdefault(listing.key, []).append(listing)

        conn = self._conn
        events: list[ChangeEvent] = []
        with conn:
            for key, records in groups.items():
                events += self._upsert_key(conn, key, records)
        return events

    def _upsert_key(
        self, conn: sqlite3.Connection, key: str, records: list[Listing]
    ) -> list[ChangeEvent]:
        incoming, incoming_sources = _merge_group(records)
        at = datetime.fromisoformat(incoming["fetched_at"])
        at_s = at.isoformat()

        row = conn.execute(
            "SELECT data, field_sources FROM listings WHERE key = ?", (key,)
        ).fetchone()
        events: list[ChangeEvent] = []
        if row is None:
            values, sources = incoming, incoming_sources
            merged = Listing.model_validate(values)
            conn.execute(
                "INSERT INTO listings (key, kind, status, price, zip, data, field_sources,"
                " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, merged.kind.value, merged.status.value, merged.price, merged.zip,
                 merged.model_dump_json(), json.dumps(sources), at_s, at_s),
            )
            events.append(ChangeEvent(key=key, kind="new", new=str(merged.price), at=at))
        else:
            stored = json.loads(row[0])
            stored_sources = json.loads(row[1])
            old = Listing.model_validate(stored)
            values, sources = _combine(stored, stored_sources, incoming, incoming_sources)
            merged = Listing.model_validate(values)
            conn.execute(
                "UPDATE listings SET status = ?, price = ?, zip = ?, data = ?,"
                " field_sources = ?, last_seen = ? WHERE key = ?",
                (merged.status.value, merged.price, merged.zip, merged.model_dump_json(),
                 json.dumps(sources), at_s, key),
            )
            if merged.price != old.price:
                events.append(ChangeEvent(
                    key=key, kind="price_change", old=str(old.price), new=str(merged.price), at=at
                ))
            if merged.status != old.status:
                events.append(ChangeEvent(
                    key=key, kind="status_change",
                    old=old.status.value, new=merged.status.value, at=at,
                ))

        for r in records:
            conn.execute(
                "INSERT INTO listing_sources (key, source, url, source_id, first_seen,"
                " last_seen, misses) VALUES (?, ?, ?, ?, ?, ?, 0)"
                " ON CONFLICT (key, source, url) DO UPDATE SET"
                " source_id = excluded.source_id, last_seen = excluded.last_seen",
                (key, r.source, r.url, r.source_id, at_s, at_s),
            )
            conn.execute(
                "UPDATE listing_sources SET misses = 0 WHERE key = ? AND source = ?",
                (key, r.source),
            )
        self._log(conn, events)
        return events

    def mark_missing(self, seen_keys: set[str], source: str) -> list[ChangeEvent]:
        """Listings a source stopped returning become OFF_MARKET after 2 misses."""
        conn = self._conn
        at = _now()
        events: list[ChangeEvent] = []
        with conn:
            rows = conn.execute(
                "SELECT DISTINCT ls.key, l.status, l.data, l.field_sources"
                " FROM listing_sources ls JOIN listings l ON l.key = ls.key"
                " WHERE ls.source = ?",
                (source,),
            ).fetchall()
            for key, status, data, field_sources in rows:
                if key in seen_keys:
                    conn.execute(
                        "UPDATE listing_sources SET misses = 0 WHERE key = ? AND source = ?",
                        (key, source),
                    )
                    continue
                if status in _CLOSED_STATUSES:
                    continue
                conn.execute(
                    "UPDATE listing_sources SET misses = misses + 1"
                    " WHERE key = ? AND source = ?",
                    (key, source),
                )
                misses = conn.execute(
                    "SELECT MAX(misses) FROM listing_sources WHERE key = ? AND source = ?",
                    (key, source),
                ).fetchone()[0]
                if misses < MISSES_BEFORE_OFF_MARKET:
                    continue
                values = json.loads(data)
                values["status"] = ListingStatus.OFF_MARKET.value
                sources = json.loads(field_sources)
                sources["status"] = f"mark_missing:{source}"
                conn.execute(
                    "UPDATE listings SET status = ?, data = ?, field_sources = ? WHERE key = ?",
                    (ListingStatus.OFF_MARKET.value, json.dumps(values), json.dumps(sources),
                     key),
                )
                events.append(ChangeEvent(
                    key=key, kind="removed", old=status,
                    new=ListingStatus.OFF_MARKET.value, at=at,
                ))
            self._log(conn, events)
        return events

    def active(self, kind: ListingKind) -> list[Listing]:
        rows = self._conn.execute(
            "SELECT data FROM listings WHERE kind = ? AND status = ? ORDER BY key",
            (ListingKind(kind).value, ListingStatus.ACTIVE.value),
        ).fetchall()
        return [Listing.model_validate_json(r[0]) for r in rows]

    def get(self, key: str) -> Listing | None:
        """The merged current record for a key, or None."""
        row = self._conn.execute("SELECT data FROM listings WHERE key = ?", (key,)).fetchone()
        return Listing.model_validate_json(row[0]) if row else None

    def source_urls(self, key: str) -> list[tuple[str, str]]:
        """Every (source, url) ever reported for a key."""
        return self._conn.execute(
            "SELECT source, url FROM listing_sources WHERE key = ? ORDER BY source, url",
            (key,),
        ).fetchall()

    # history

    @staticmethod
    def _log(conn: sqlite3.Connection, events: list[ChangeEvent]) -> None:
        field = {"new": "listing", "price_change": "price"}
        conn.executemany(
            "INSERT INTO history (key, kind, field, old, new, at) VALUES (?, ?, ?, ?, ?, ?)",
            [(e.key, e.kind, field.get(e.kind, "status"), e.old, e.new,
              _aware(e.at).astimezone(UTC).isoformat()) for e in events],
        )

    def changes_since(self, since: date) -> list[ChangeEvent]:
        rows = self._conn.execute(
            "SELECT key, kind, old, new, at FROM history"
            " WHERE substr(at, 1, 10) >= ? ORDER BY at, id",
            (since.isoformat(),),
        ).fetchall()
        return [
            ChangeEvent(key=k, kind=kind, old=old, new=new, at=datetime.fromisoformat(at))
            for k, kind, old, new, at in rows
        ]

    # alerts

    def was_alerted(self, key: str, price: int) -> bool:
        """True if a hot alert already went out for this key at this price."""
        row = self._conn.execute(
            "SELECT 1 FROM alerts WHERE key = ? AND price = ?", (key, price)
        ).fetchone()
        return row is not None

    def record_alert(self, key: str, price: int) -> None:
        conn = self._conn
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO alerts (key, price, at) VALUES (?, ?, ?)",
                (key, price, _now().isoformat()),
            )

    # snapshots

    def save_snapshot(self, snap: MarketSnapshot) -> None:
        conn = self._conn
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (area_id, week_of, active_count,"
                " median_price, median_ppsf, median_dom, median_rent, price_cut_share)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snap.area_id, snap.week_of.isoformat(), snap.active_count, snap.median_price,
                 snap.median_ppsf, snap.median_dom, snap.median_rent, snap.price_cut_share),
            )

    def snapshots(self, area_id: str, weeks: int = 26) -> list[MarketSnapshot]:
        """The area's most recent `weeks` snapshots, oldest first."""
        rows = self._conn.execute(
            "SELECT area_id, week_of, active_count, median_price, median_ppsf, median_dom,"
            " median_rent, price_cut_share FROM snapshots WHERE area_id = ?"
            " ORDER BY week_of DESC LIMIT ?",
            (area_id, weeks),
        ).fetchall()
        cols = ("area_id", "week_of", "active_count", "median_price", "median_ppsf",
                "median_dom", "median_rent", "price_cut_share")
        return [MarketSnapshot.model_validate(dict(zip(cols, r, strict=True)))
                for r in reversed(rows)]

    # meta

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        conn = self._conn
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
