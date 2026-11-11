"""School zoning (WP5).

Boundary polygons here are SYNTHETIC rectangles in tests/fixtures/boundaries/,
named after the real target schools and placed roughly where those schools are.
They are not real attendance zones: the real files could not be downloaded from
the build session. Once data/boundaries/*.geojson exist, add real-address cases.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from home_search.config import load_search
from home_search.enrich import schools
from home_search.enrich.schools import (
    area_for,
    is_target,
    missing_boundaries,
    same_school,
    zone,
)
from home_search.models import Listing, ListingKind, SchoolZoning, ZoningConfidence

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "boundaries"

# Points inside the synthetic rectangles.
IN_BELLAIRE_HS = (29.705, -95.46)     # also Condit El, Pershing MS
IN_LAMAR_HS = (29.70, -95.42)
IN_MEMORIAL_HS = (29.775, -95.51)     # also Bunker Hill El, Memorial MS
IN_STRATFORD_HS = (29.775, -95.57)
OUTSIDE_ALL = (29.60, -95.20)


@pytest.fixture(scope="module")
def cfg():
    return load_search()


def make(
    zip_: str = "77401",
    point: tuple[float, float] | None = None,
    description: str | None = None,
    har_schools: dict[str, str] | None = None,
) -> Listing:
    lat, lon = point if point else (None, None)
    return Listing(
        source="test",
        source_id="1",
        url="https://example.invalid/1",
        kind=ListingKind.SALE,
        address="100 Example St",
        city="Houston",
        zip=zip_,
        lat=lat,
        lon=lon,
        price=1_000_000,
        description=description,
        har_schools=har_schools,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------- verified


def test_inside_bellaire_high_is_verified_target(cfg):
    z = zone(make("77401", IN_BELLAIRE_HS), cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.VERIFIED
    assert z.district == "HISD"
    assert z.high == "Bellaire High School"
    assert z.middle == "Pershing Middle School"
    assert z.elementary == "Condit Elementary"   # canonicalized to the config spelling
    assert z.boundary_vintage == "2026-2027"
    assert z.ratings == {}
    assert is_target(z, cfg.area("bellaire"), cfg)


def test_inside_memorial_high_is_verified_target(cfg):
    z = zone(make("77024", IN_MEMORIAL_HS), cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.VERIFIED
    assert z.district == "SBISD"
    assert z.high == "Memorial High School"      # file says "MEMORIAL H S"
    assert z.elementary == "Bunker Hill Elementary"
    assert z.middle == "Memorial Middle School"
    assert is_target(z, cfg.area("memorial"), cfg)
    assert not is_target(z, cfg.area("bellaire"), cfg)


def test_target_zip_but_non_target_high_school_hisd(cfg):
    listing = make("77025", IN_LAMAR_HS)          # 77025 is a Bellaire fringe zip
    z = zone(listing, cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.VERIFIED
    assert z.high == "Lamar High School"
    area = area_for(listing, z, cfg)
    assert area is not None and area.id == "bellaire"
    assert not is_target(z, area, cfg)


def test_target_zip_but_non_target_high_school_sbisd(cfg):
    listing = make("77024", IN_STRATFORD_HS)      # core Memorial zip, Stratford zone
    z = zone(listing, cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.VERIFIED
    assert z.high == "Stratford High School"
    assert not is_target(z, cfg.area("memorial"), cfg)


def test_polygon_beats_listing_claim(cfg):
    listing = make(
        "77024", IN_STRATFORD_HS,
        description="Zoned to Memorial High School!",
        har_schools={"high": "Memorial HS"},
    )
    z = zone(listing, cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.VERIFIED
    assert z.high == "Stratford High School"


# ------------------------------------------------------- unknown and claims


def test_no_coordinates_is_unknown(cfg):
    z = zone(make("77401"), cfg, FIXTURE_DIR)
    assert z == SchoolZoning(confidence=ZoningConfidence.UNKNOWN)
    assert not is_target(z, cfg.area("bellaire"), cfg)


def test_outside_every_polygon_is_unknown_not_zip_guess(cfg):
    z = zone(make("77401", OUTSIDE_ALL), cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.UNKNOWN
    assert z.high is None


@pytest.mark.parametrize("point", [None, OUTSIDE_ALL])
def test_description_claim_without_polygon_match(cfg, point):
    listing = make("77401", point, description="Charming home. Zoned to Bellaire H.S. per HISD.")
    z = zone(listing, cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.LISTING_CLAIM
    assert z.high == "Bellaire High School"
    assert z.district == "HISD"
    assert not is_target(z, cfg.area("bellaire"), cfg)
    lenient = cfg.model_copy(
        update={"schools": cfg.schools.model_copy(update={"require_verified_high": False})}
    )
    assert is_target(z, cfg.area("bellaire"), lenient)


def test_har_schools_claim(cfg):
    listing = make("77024", har_schools={"elementary": "Bunker Hill El", "high": "Memorial HS"})
    z = zone(listing, cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.LISTING_CLAIM
    assert z.high == "Memorial High School"
    assert z.elementary == "Bunker Hill Elementary"
    assert z.district == "SBISD"


def test_har_schools_naming_only_non_targets_is_unknown(cfg):
    listing = make("77079", har_schools={"elementary": "", "high": "Stratford High School"})
    assert zone(listing, cfg, FIXTURE_DIR).confidence == ZoningConfidence.UNKNOWN


@pytest.mark.parametrize(
    "text",
    [
        "Walk to shops in Bellaire. High ceilings throughout.",
        "Minutes from Memorial Park and Memorial City.",
        "Near Memorial Drive, high-end finishes.",
        None,
    ],
)
def test_description_without_target_school_is_unknown(cfg, text):
    z = zone(make("77401", description=text), cfg, FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.UNKNOWN


def test_description_names_elementary_variant(cfg):
    z = zone(make("77024", description="Zoned to award-winning Hunters Creek Elem."), cfg,
             FIXTURE_DIR)
    assert z.confidence == ZoningConfidence.LISTING_CLAIM
    assert z.elementary == "Hunters Creek Elementary"
    assert z.high is None


# ---------------------------------------------------------- missing files


def test_missing_boundary_file_is_unknown_and_reported(cfg, tmp_path):
    shutil.copy(FIXTURE_DIR / "hisd.geojson", tmp_path / "hisd.geojson")
    z = zone(make("77024", IN_MEMORIAL_HS), cfg, tmp_path)
    assert z.confidence == ZoningConfidence.UNKNOWN
    assert missing_boundaries(cfg, tmp_path) == ["sbisd.geojson"]
    # the district that does have a file still verifies
    assert zone(make("77401", IN_BELLAIRE_HS), cfg, tmp_path).confidence == "verified"


def test_no_boundary_files_at_all(cfg, tmp_path):
    listing = make("77024", IN_MEMORIAL_HS, description="Memorial HS zoned")
    z = zone(listing, cfg, tmp_path)
    assert z.confidence == ZoningConfidence.LISTING_CLAIM
    assert sorted(missing_boundaries(cfg, tmp_path)) == ["hisd.geojson", "sbisd.geojson"]
    assert sorted(missing_boundaries(boundary_dir=tmp_path)) == ["hisd.geojson", "sbisd.geojson"]


def test_all_boundaries_present(cfg):
    assert missing_boundaries(cfg, FIXTURE_DIR) == []


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"type": "FeatureCollection", "features": []}),
        # projected coordinates (state plane feet), not lon/lat
        json.dumps({"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "properties": {"school": "Memorial High School", "level": "high"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[3_000_000, 13_800_000], [3_010_000, 13_800_000],
                 [3_010_000, 13_810_000], [3_000_000, 13_800_000]]]},
        }]}),
    ],
)
def test_unusable_boundary_file_counts_as_missing(cfg, tmp_path, content):
    (tmp_path / "sbisd.geojson").write_text(content)
    shutil.copy(FIXTURE_DIR / "hisd.geojson", tmp_path / "hisd.geojson")
    assert zone(make("77024", IN_MEMORIAL_HS), cfg, tmp_path).confidence == "unknown"
    assert missing_boundaries(cfg, tmp_path) == ["sbisd.geojson"]


def test_features_missing_properties_are_skipped(cfg, tmp_path):
    data = json.loads((FIXTURE_DIR / "sbisd.geojson").read_text())
    data["features"].append({"type": "Feature", "properties": {"school": "No Level"},
                             "geometry": data["features"][0]["geometry"]})
    (tmp_path / "sbisd.geojson").write_text(json.dumps(data))
    z = zone(make("77024", IN_MEMORIAL_HS), cfg, tmp_path)
    assert z.high == "Memorial High School"


def test_boundaries_load_once_per_process(cfg):
    schools._load_cached.cache_clear()
    for _ in range(3):
        zone(make("77401", IN_BELLAIRE_HS), cfg, FIXTURE_DIR)
    info = schools._load_cached.cache_info()
    assert info.misses == 1   # hisd parsed once (it matches first, sbisd never needed)
    assert info.hits == 2


# ------------------------------------------------------------ name matching


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Bellaire HS", "Bellaire High School"),
        ("BELLAIRE H S", "Bellaire High School"),
        ("bellaire h.s.", "Bellaire High School"),
        ("Bellaire High", "Bellaire High School"),
        ("Bellaire Senior High School", "Bellaire High School"),
        ("Condit El", "Condit Elementary"),
        ("Condit Elem.", "Condit Elementary"),
        ("CONDIT ELEMENTARY SCHOOL", "Condit Elementary"),
        ("Pin Oak MS", "Pin Oak Middle School"),
        ("Jane Long Academy", "Jane Long Academy"),
    ],
)
def test_same_school_variants(a, b):
    assert same_school(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Memorial Middle School", "Memorial High School"),
        ("Memorial Drive Elementary", "Memorial High School"),
        ("Stratford HS", "Memorial High School"),
        ("Bellaire Elementary", "Bellaire High School"),
        ("", "Bellaire High School"),
        (None, "Bellaire High School"),
    ],
)
def test_same_school_rejects(a, b):
    assert not same_school(a, b)


# ---------------------------------------------------------------- area_for


def test_area_for_by_zip_when_unverified(cfg):
    unknown = SchoolZoning()
    assert area_for(make("77401"), unknown, cfg).id == "bellaire"
    assert area_for(make("77096"), unknown, cfg).id == "bellaire"     # fringe
    assert area_for(make("77055"), unknown, cfg).id == "memorial"
    assert area_for(make("77079"), unknown, cfg).id == "memorial"     # fringe
    assert area_for(make("77002"), unknown, cfg) is None


def test_area_for_prefers_verified_district_over_zip(cfg):
    z = SchoolZoning(district="SBISD", high="Memorial High School",
                     confidence=ZoningConfidence.VERIFIED)
    assert area_for(make("77401"), z, cfg).id == "memorial"


def test_area_for_ignores_claimed_district(cfg):
    z = SchoolZoning(district="SBISD", high="Memorial High School",
                     confidence=ZoningConfidence.LISTING_CLAIM)
    assert area_for(make("77401"), z, cfg).id == "bellaire"


def test_is_target_rejects_wrong_district(cfg):
    z = SchoolZoning(district="SBISD", high="Bellaire High School",
                     confidence=ZoningConfidence.VERIFIED)
    assert not is_target(z, cfg.area("bellaire"), cfg)
