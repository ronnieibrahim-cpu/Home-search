"""SQLite state: listings seen, their history, sent alerts, market snapshots. WP2.

The database holds listing data only. Nothing from the profile is ever stored.
Locally it lives at data/state.sqlite (gitignored); in CI the workflow restores
it from, and pushes it back to, the orphan `state` branch.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .models import ChangeEvent, Listing, ListingKind, MarketSnapshot

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "state.sqlite"


def normalize_key(address: str, zip_code: str, kind: ListingKind) -> str:
    """Stable cross-source identity for a property.

    Must collapse "123 N Main St", "123 North Main Street" and "123 N. MAIN ST
    #A" vs "Unit A" to one key, and must keep sale and rent listings apart.
    """
    raise NotImplementedError("WP2")


class Store:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path

    def migrate(self) -> None:
        """Create or upgrade the schema. Idempotent."""
        raise NotImplementedError("WP2")

    def upsert(self, listings: list[Listing]) -> list[ChangeEvent]:
        """Merge a run's listings; return what is new or changed since last run.

        When two sources report the same key, prefer the record with more
        non-null fields but keep every source URL.
        """
        raise NotImplementedError("WP2")

    def mark_missing(self, seen_keys: set[str], source: str) -> list[ChangeEvent]:
        """Listings a source stopped returning become OFF_MARKET after 2 misses."""
        raise NotImplementedError("WP2")

    def active(self, kind: ListingKind) -> list[Listing]:
        raise NotImplementedError("WP2")

    def changes_since(self, since: date) -> list[ChangeEvent]:
        raise NotImplementedError("WP2")

    def was_alerted(self, key: str, price: int) -> bool:
        """True if a hot alert already went out for this key at this price."""
        raise NotImplementedError("WP2")

    def record_alert(self, key: str, price: int) -> None:
        raise NotImplementedError("WP2")

    def save_snapshot(self, snap: MarketSnapshot) -> None:
        raise NotImplementedError("WP2")

    def snapshots(self, area_id: str, weeks: int = 26) -> list[MarketSnapshot]:
        raise NotImplementedError("WP2")
