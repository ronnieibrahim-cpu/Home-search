"""WP2: SQLite store. Synthetic listing data only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from home_search.models import Listing, ListingKind, ListingStatus, MarketSnapshot
from home_search.store import Store, normalize_key

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make(**kw) -> Listing:
    base = {
        "source": "redfin_csv", "source_id": "r1", "url": "https://example.test/r1",
        "kind": ListingKind.SALE, "address": "100 Example St", "city": "Houston",
        "zip": "77401", "price": 900_000, "fetched_at": T0,
    }
    base.update(kw)
    return Listing(**base)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


# --- normalize_key -----------------------------------------------------------

SALE = ListingKind.SALE


@pytest.mark.parametrize("a, b", [
    ("123 N Main St", "123 North Main Street"),
    ("123 N. MAIN ST #A", "123 N Main St Unit A"),
    ("123 N. MAIN ST #A", "123 North Main Street, Unit A"),
    ("123 N Main St Apt A", "123 N Main St # A"),
    ("123 N Main St Unit #A", "123 n main st #a"),
    ("123 N Main St", "123 N Main St, Houston, TX 77401"),
    ("4500 Bissonnet Avenue", "4500 BISSONNET AVE"),
    ("12 Memorial Drive", "12  memorial dr."),
    ("5 S Rice Boulevard", "5 South Rice Blvd"),
])
def test_key_equivalences(a, b):
    assert normalize_key(a, "77401", SALE) == normalize_key(b, "77401", SALE)


def test_key_distinctions():
    base = normalize_key("123 N Main St", "77401", SALE)
    assert normalize_key("123 N Main St #A", "77401", SALE) != base
    assert normalize_key("123 N Main St #A", "77401", SALE) != normalize_key(
        "123 N Main St #B", "77401", SALE)
    assert normalize_key("123 S Main St", "77401", SALE) != base
    assert normalize_key("125 N Main St", "77401", SALE) != base
    assert normalize_key("123 N Main St", "77024", SALE) != base


def test_key_keeps_sale_and_rent_apart():
    assert normalize_key("123 N Main St", "77401", ListingKind.SALE) != normalize_key(
        "123 N Main St", "77401", ListingKind.RENT)


def test_key_zip_plus_four():
    assert normalize_key("1 A St", "77401-1234", SALE) == normalize_key("1 A St", "77401", SALE)


def test_listing_key_property_uses_normalize_key():
    a = make(address="123 North Main Street")
    b = make(address="123 N. MAIN ST", source="email_har")
    assert a.key == b.key


# --- migrate -----------------------------------------------------------------

def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "sub" / "state.sqlite"
    s = Store(path)
    s.migrate()
    s.upsert([make()])
    s.migrate()
    s.close()
    s2 = Store(path)
    s2.migrate()
    s2.migrate()
    assert len(s2.active(SALE)) == 1
    s2.close()


# --- upsert events -----------------------------------------------------------

def test_new_listing_event(store):
    events = store.upsert([make()])
    assert [e.kind for e in events] == ["new"]
    assert events[0].new == "900000"
    assert store.upsert([make(fetched_at=T0 + timedelta(days=1))]) == []


def test_price_change_event(store):
    store.upsert([make()])
    events = store.upsert([make(price=875_000, fetched_at=T0 + timedelta(days=1))])
    assert [(e.kind, e.old, e.new) for e in events] == [("price_change", "900000", "875000")]
    assert store.active(SALE)[0].price == 875_000


def test_status_change_event(store):
    store.upsert([make()])
    events = store.upsert([make(status=ListingStatus.PENDING, fetched_at=T0 + timedelta(1))])
    assert [(e.kind, e.old, e.new) for e in events] == [("status_change", "active", "pending")]
    assert store.active(SALE) == []


def test_price_and_status_change_together(store):
    store.upsert([make()])
    events = store.upsert([make(price=1, status=ListingStatus.SOLD, fetched_at=T0)])
    assert sorted(e.kind for e in events) == ["price_change", "status_change"]


def test_changes_since(store):
    store.upsert([make()])
    store.upsert([make(price=850_000, fetched_at=T0 + timedelta(days=5))])
    all_events = store.changes_since(date(2026, 11, 1))
    assert [e.kind for e in all_events] == ["new", "price_change"]
    later = store.changes_since(date(2026, 11, 3))
    assert [e.kind for e in later] == ["price_change"]
    assert store.changes_since(date(2026, 12, 1)) == []


# --- merging sources ---------------------------------------------------------

def test_two_sources_merge_to_one_row(store):
    redfin = make(lat=29.7, lon=-95.46, sqft=None, beds=4)
    har = make(source="email_har", source_id="h1", url="https://example.test/h1",
               address="100 Example Street", sqft=2800, beds=None)
    events = store.upsert([redfin, har])
    assert [e.kind for e in events] == ["new"]
    rows = store.active(SALE)
    assert len(rows) == 1
    merged = rows[0]
    assert (merged.sqft, merged.beds, merged.lat) == (2800, 4, 29.7)
    assert merged.source == "email_har"
    assert store.source_urls(redfin.key) == [
        ("email_har", "https://example.test/h1"),
        ("redfin_csv", "https://example.test/r1"),
    ]


def test_har_field_wins_in_same_run(store):
    redfin = make(price=905_000, sqft=2900, lat=29.7, lon=-95.4, beds=4, baths=3.5,
                  year_built=2005)
    har = make(source="har_web", url="https://example.test/h", price=899_000, sqft=2850)
    store.upsert([redfin, har])
    merged = store.get(redfin.key)
    assert (merged.price, merged.sqft) == (899_000, 2850)
    assert (merged.beds, merged.year_built) == (4, 2005)


def test_har_field_wins_across_runs(store):
    har = make(source="har_browser", url="https://example.test/h", sqft=2850)
    store.upsert([har])
    events = store.upsert([make(sqft=2900, price=905_000, lat=29.7,
                                fetched_at=T0 + timedelta(days=1))])
    # Static HAR fields hold; price is volatile, so the newer report wins.
    assert [e.kind for e in events] == ["price_change"]
    merged = store.get(har.key)
    assert (merged.sqft, merged.price, merged.lat) == (2850, 905_000, 29.7)


def test_non_har_later_value_replaces_non_har(store):
    store.upsert([make(sqft=2800)])
    store.upsert([make(source="email_redfin", url="https://example.test/e", sqft=None,
                       fetched_at=T0 + timedelta(days=1))])
    assert store.get(make().key).sqft == 2800      # null does not overwrite
    store.upsert([make(sqft=2850, fetched_at=T0 + timedelta(days=2))])
    assert store.get(make().key).sqft == 2850


def test_more_complete_record_wins_between_non_har_sources(store):
    sparse = make(source="email_redfin", url="https://example.test/e", price=910_000)
    full = make(price=900_000, sqft=2800, beds=4, baths=3)
    store.upsert([sparse, full])
    assert store.get(full.key).price == 900_000


def test_source_urls_accumulate(store):
    store.upsert([make()])
    store.upsert([make(url="https://example.test/r1-moved", fetched_at=T0 + timedelta(1))])
    assert len(store.source_urls(make().key)) == 2


# --- mark_missing ------------------------------------------------------------

def test_two_miss_rule(store):
    a = make()
    b = make(address="200 Other St", source_id="r2", url="https://example.test/r2")
    store.upsert([a, b])

    assert store.mark_missing({a.key}, "redfin_csv") == []          # first miss
    assert len(store.active(SALE)) == 2
    events = store.mark_missing({a.key}, "redfin_csv")              # second miss
    assert [(e.key, e.kind, e.old, e.new) for e in events] == [
        (b.key, "removed", "active", "off_market")]
    assert [x.key for x in store.active(SALE)] == [a.key]
    assert store.get(b.key).status == ListingStatus.OFF_MARKET
    assert store.mark_missing({a.key}, "redfin_csv") == []          # no repeat
    assert "removed" in [e.kind for e in store.changes_since(date(2026, 1, 1))]


def test_seen_again_resets_miss_count(store):
    a = make()
    store.upsert([a])
    store.mark_missing(set(), "redfin_csv")
    store.mark_missing({a.key}, "redfin_csv")
    assert store.mark_missing(set(), "redfin_csv") == []            # count restarted
    assert len(store.active(SALE)) == 1


def test_upsert_resets_miss_count(store):
    a = make()
    store.upsert([a])
    store.mark_missing(set(), "redfin_csv")
    store.upsert([a])
    assert store.mark_missing(set(), "redfin_csv") == []


def test_missing_only_counts_for_that_source(store):
    har = make(source="email_har", url="https://example.test/h")
    store.upsert([har])
    store.mark_missing(set(), "redfin_csv")
    assert store.mark_missing(set(), "redfin_csv") == []
    assert len(store.active(SALE)) == 1


def test_off_market_listing_returns(store):
    a = make()
    store.upsert([a])
    store.mark_missing(set(), "redfin_csv")
    store.mark_missing(set(), "redfin_csv")
    events = store.upsert([make(fetched_at=T0 + timedelta(days=3))])
    assert [(e.kind, e.old, e.new) for e in events] == [
        ("status_change", "off_market", "active")]


# --- alerts ------------------------------------------------------------------

def test_alert_dedup_and_realert_after_cut(store):
    key = make().key
    assert not store.was_alerted(key, 900_000)
    store.record_alert(key, 900_000)
    store.record_alert(key, 900_000)                # same price twice: no error
    assert store.was_alerted(key, 900_000)
    assert not store.was_alerted(key, 875_000)      # price cut re-alerts
    store.record_alert(key, 875_000)
    assert store.was_alerted(key, 875_000)


# --- snapshots ---------------------------------------------------------------

def snap(week: date, count: int = 10) -> MarketSnapshot:
    return MarketSnapshot(area_id="bellaire", week_of=week, active_count=count,
                          median_price=950_000, median_ppsf=390.5, median_dom=30,
                          median_rent=None, price_cut_share=0.2)


def test_snapshots_roundtrip_and_window(store):
    start = date(2026, 1, 5)
    for i in range(30):
        store.save_snapshot(snap(start + timedelta(weeks=i), count=i))
    store.save_snapshot(snap(start, count=99))      # same week replaces
    store.save_snapshot(snap(start, count=99).model_copy(update={"area_id": "memorial"}))
    got = store.snapshots("bellaire", weeks=4)
    assert [s.active_count for s in got] == [26, 27, 28, 29]
    assert got[0].median_ppsf == 390.5 and got[0].median_rent is None
    assert len(store.snapshots("bellaire")) == 26
    assert store.snapshots("memorial")[0].active_count == 99


# --- meta --------------------------------------------------------------------

def test_meta(store):
    assert store.get_meta("imap_uid") is None
    store.set_meta("imap_uid", "41")
    store.set_meta("imap_uid", "42")
    assert store.get_meta("imap_uid") == "42"
