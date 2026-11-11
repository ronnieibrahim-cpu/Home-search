"""School zoning by point-in-polygon against district attendance boundaries. WP5.

Listing sites' school fields are frequently wrong at zone edges, and the whole
search hinges on zoning, so the boundary file is the only thing that can set
ZoningConfidence.VERIFIED. Boundary GeoJSON lives in data/boundaries/ (see the
README there for sourcing and the yearly refresh).

One file per district, named after `Area.district` in lower case
(`SBISD` -> `sbisd.geojson`). Each feature is one school's zone with properties
`school`, `level` (elementary | middle | high) and `school_year`, in WGS84
lon/lat. A missing or unreadable file is not an error: listings in that district
come back UNKNOWN (or LISTING_CLAIM) and `missing_boundaries()` names the file so
the digest footer can say so.

Ratings: no public ratings source is available without scraping, so
`SchoolZoning.ratings` is left empty.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from shapely import make_valid
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from ..config import ROOT, Area, SearchConfig
from ..models import Listing, SchoolZoning, ZoningConfidence

log = logging.getLogger(__name__)

DEFAULT_BOUNDARY_DIR = ROOT / "data" / "boundaries"
LEVELS = ("elementary", "middle", "high")
DEFAULT_DISTRICTS = ("HISD", "SBISD")

# Level suffixes, normalized (lower case, punctuation -> space), longest first.
_SUFFIXES: dict[str, tuple[str, ...]] = {
    "high": (
        "senior high school", "senior high", "high school", "h s", "hs", "high", "shs",
    ),
    "middle": ("middle school", "m s", "ms", "middle", "intermediate"),
    "elementary": ("elementary school", "elementary", "elem", "el", "e s", "es"),
}


# ----------------------------------------------------------------- name matching


def _norm(text: str) -> str:
    """Lower case, punctuation to spaces, single-spaced."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _split_suffix(name: str) -> tuple[str, str | None]:
    """('bellaire', 'high') for 'Bellaire H.S.'; (name, None) when no level suffix."""
    n = _norm(name)
    for level, suffixes in _SUFFIXES.items():
        for suf in suffixes:
            if n.endswith(" " + suf):
                return n[: -len(suf) - 1].strip(), level
    if n.endswith(" school"):
        return n[: -len(" school")].strip(), None
    return n, None


def school_key(name: str) -> str:
    """Comparable form of a school name with any level suffix removed."""
    return _split_suffix(name)[0]


def same_school(a: str | None, b: str | None) -> bool:
    """True when two names refer to the same school at the same level.

    Tolerates case, punctuation and "High School"/"HS"/"H S",
    "Elementary"/"El"/"Elem", "Middle School"/"MS" variants. Callers compare names
    within one level; a stated level that differs (Memorial HS vs Memorial MS)
    is not a match.
    """
    if not a or not b:
        return False
    base_a, lvl_a = _split_suffix(a)
    base_b, lvl_b = _split_suffix(b)
    if lvl_a and lvl_b and lvl_a != lvl_b:
        return False
    return bool(base_a) and base_a == base_b


def _canonical(name: str, candidates: Iterable[str]) -> str:
    """The config spelling of `name` when it is one of `candidates`, else `name`."""
    return next((c for c in candidates if same_school(name, c)), name)


def _targets_by_level(cfg: SearchConfig) -> dict[str, list[tuple[str, str]]]:
    """level -> [(school name, area district)] across all areas."""
    out: dict[str, list[tuple[str, str]]] = {lvl: [] for lvl in LEVELS}
    for area in cfg.areas:
        ts = area.target_schools
        for lvl, names in (("high", ts.high), ("middle", ts.middle),
                           ("elementary", ts.elementary)):
            out[lvl].extend((n, area.district) for n in names)
    return out


def _description_pattern(name: str, level: str) -> re.Pattern[str]:
    base, stated = _split_suffix(name)
    words = r"\s+".join(re.escape(w) for w in base.split())
    if stated is None:  # e.g. "Jane Long Academy": match the whole name only
        return re.compile(rf"\b{words}\b")
    sufs = "|".join(r"\s+".join(re.escape(w) for w in s.split()) for s in _SUFFIXES[level])
    return re.compile(rf"\b{words}\s+(?:{sufs})\b")


def _description_claims(text: str, cfg: SearchConfig) -> dict[str, tuple[str, str]]:
    """level -> (target school, district) named in free text.

    Text is split at sentence and clause breaks first so that
    "...in Bellaire. High ceilings" is not read as "Bellaire High".
    """
    chunks = [_norm(c) for c in re.split(r"[!?;,:\n]+|\.\s+|\.$", text)]
    found: dict[str, tuple[str, str]] = {}
    for level, targets in _targets_by_level(cfg).items():
        for name, district in targets:
            pat = _description_pattern(name, level)
            if any(pat.search(c) for c in chunks):
                found.setdefault(level, (name, district))
    return found


# --------------------------------------------------------------- boundary files


@dataclass(frozen=True)
class _Zone:
    school: str
    level: str
    school_year: str | None


@dataclass(frozen=True)
class _DistrictBoundaries:
    zones: tuple[_Zone, ...]
    tree: STRtree

    def lookup(self, lon: float, lat: float) -> dict[str, _Zone]:
        """level -> zone containing the point (first by file order on a shared edge)."""
        hits: dict[str, _Zone] = {}
        for i in sorted(int(i) for i in self.tree.query(Point(lon, lat), predicate="intersects")):
            z = self.zones[i]
            hits.setdefault(z.level, z)
        return hits


def boundary_file(district: str, boundary_dir: Path | None = None) -> Path:
    return (boundary_dir or DEFAULT_BOUNDARY_DIR) / f"{district.lower()}.geojson"


def _parse(path: Path) -> _DistrictBoundaries:
    data = json.loads(path.read_text())
    if data.get("type") != "FeatureCollection":
        raise ValueError("not a GeoJSON FeatureCollection")
    zones: list[_Zone] = []
    geoms = []
    for n, feat in enumerate(data.get("features") or []):
        props = feat.get("properties") or {}
        school = (props.get("school") or "").strip()
        level = (props.get("level") or "").strip().lower()
        geom_json = feat.get("geometry")
        if not school or level not in LEVELS or not geom_json:
            log.warning("%s: feature %d skipped (needs school, level, geometry)", path.name, n)
            continue
        geom = shape(geom_json)
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            log.warning("%s: feature %d skipped (geometry is %s)", path.name, n, geom.geom_type)
            continue
        if not geom.is_valid:
            geom = make_valid(geom)
        minx, miny, maxx, maxy = geom.bounds
        if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
            raise ValueError("coordinates are not WGS84 lon/lat (reproject to EPSG:4326)")
        year = props.get("school_year")
        zones.append(_Zone(school, level, str(year) if year else None))
        geoms.append(geom)
    if not zones:
        raise ValueError("no usable features")
    return _DistrictBoundaries(tuple(zones), STRtree(geoms))


@lru_cache(maxsize=16)
def _load_cached(path_str: str, mtime_ns: int) -> _DistrictBoundaries | None:
    path = Path(path_str)
    try:
        return _parse(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        log.warning("boundary file %s unusable: %s", path.name, e)
        return None


def _load(district: str, boundary_dir: Path | None) -> _DistrictBoundaries | None:
    """Parsed boundaries for a district, loaded once per process per file version."""
    path = boundary_file(district, boundary_dir)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return _load_cached(str(path.resolve()), mtime)


def _districts(cfg: SearchConfig | None) -> list[str]:
    if cfg is None:
        return list(DEFAULT_DISTRICTS)
    return list(dict.fromkeys(a.district for a in cfg.areas))


def missing_boundaries(
    cfg: SearchConfig | None = None, boundary_dir: Path | None = None
) -> list[str]:
    """File names of district boundary files that are absent or unusable.

    Districts come from cfg.areas (default: HISD and SBISD). An empty list means
    every district can be VERIFIED. Meant for the digest footer.
    """
    return [
        boundary_file(d, boundary_dir).name
        for d in _districts(cfg)
        if _load(d, boundary_dir) is None
    ]


# ------------------------------------------------------------------------ zoning


def _verified(listing: Listing, cfg: SearchConfig, boundary_dir: Path | None) -> SchoolZoning | None:
    if listing.lat is None or listing.lon is None:
        return None
    targets = _targets_by_level(cfg)
    for district in _districts(cfg):
        bounds = _load(district, boundary_dir)
        if bounds is None:
            continue
        hits = bounds.lookup(listing.lon, listing.lat)
        if "high" not in hits:  # only the high-school polygon settles zoning
            continue
        names = {
            lvl: _canonical(z.school, (n for n, _ in targets[lvl])) for lvl, z in hits.items()
        }
        return SchoolZoning(
            district=district,
            elementary=names.get("elementary"),
            middle=names.get("middle"),
            high=names["high"],
            confidence=ZoningConfidence.VERIFIED,
            boundary_vintage=hits["high"].school_year,
        )
    return None


def _claimed(listing: Listing, cfg: SearchConfig) -> SchoolZoning | None:
    targets = _targets_by_level(cfg)
    names: dict[str, str] = {}
    districts: list[str] = []
    for lvl, raw in (listing.har_schools or {}).items():
        lvl = lvl.lower()
        if lvl not in LEVELS or not raw or not raw.strip():
            continue
        match = next(((n, d) for n, d in targets[lvl] if same_school(raw, n)), None)
        if match:
            names[lvl] = match[0]
            districts.append(match[1])
        else:
            names[lvl] = raw.strip()
    if listing.description:
        for lvl, (name, district) in _description_claims(listing.description, cfg).items():
            if lvl not in names or not any(same_school(names[lvl], n) for n, _ in targets[lvl]):
                names[lvl] = name
            districts.append(district)
    if not districts:  # nothing names a target school
        return None
    return SchoolZoning(
        district=districts[0],
        elementary=names.get("elementary"),
        middle=names.get("middle"),
        high=names.get("high"),
        confidence=ZoningConfidence.LISTING_CLAIM,
    )


def zone(
    listing: Listing, cfg: SearchConfig, boundary_dir: Path | None = None
) -> SchoolZoning:
    """Resolve elementary / middle / high for a listing.

    No lat/lon, or no polygon hit -> confidence UNKNOWN (or LISTING_CLAIM when
    the description or har_schools names a target school). Never guess from zip
    code. A missing boundary file is not an error; see missing_boundaries().
    `boundary_dir` defaults to data/boundaries/.
    """
    return (
        _verified(listing, cfg, boundary_dir)
        or _claimed(listing, cfg)
        or SchoolZoning(confidence=ZoningConfidence.UNKNOWN)
    )


def is_target(zoning: SchoolZoning, area: Area, cfg: SearchConfig) -> bool:
    """True when the high school is a target and, per cfg.schools, verified.

    Elementary and middle matches are informational only (scoring uses them).
    """
    if not any(same_school(zoning.high, t) for t in area.target_schools.high):
        return False
    if zoning.confidence == ZoningConfidence.VERIFIED:
        return zoning.district is None or zoning.district.lower() == area.district.lower()
    return not cfg.schools.require_verified_high


def area_for(listing: Listing, zoning: SchoolZoning, cfg: SearchConfig) -> Area | None:
    """The search area a listing belongs to.

    By verified district (and target high school, if two areas share a district)
    when available; otherwise by the listing's zip in an area's zips or
    fringe_zips. None when neither places it.
    """
    if zoning.confidence == ZoningConfidence.VERIFIED and zoning.district:
        same = [a for a in cfg.areas if a.district.lower() == zoning.district.lower()]
        if len(same) > 1:
            same = [
                a for a in same if any(same_school(zoning.high, t) for t in a.target_schools.high)
            ] or same
        if same:
            return same[0]
    zip5 = (listing.zip or "").strip()[:5]
    return next((a for a in cfg.areas if zip5 in a.zips), None) or next(
        (a for a in cfg.areas if zip5 in a.fringe_zips), None
    )
