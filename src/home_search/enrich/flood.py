"""Flood risk. WP6.

Both target areas have real flood history (Brays Bayou through Bellaire;
Buffalo Bayou and the Addicks/Barker reservoir releases along Memorial), so
flood is a first-class score component and an insurance cost input, not a note.

Primary signal: FEMA National Flood Hazard Layer zone at the listing's lat/lon
(public ArcGIS REST query, cached per coordinate in data/cache/). Secondary:
keywords in the listing description ("never flooded", "flooded", "remediated",
"elevated"). A FEMA zone of X does not mean the house has not flooded; the
digest must say so and tell the reader to request the seller's disclosure.

Zone mapping (FEMA field -> our value). The owner accepts only unshaded X, so
anything that is not positively identified as minimal hazard is never "X":

    ZONE_SUBTY contains FLOODWAY / ENCROACHMENT  -> "floodway", in_floodway=True
    FLD_ZONE A, AE, AH, AO, AR, A99, A1-A30,
             V, VE, V1-V30, or SFHA_TF == "T"    -> "AE"
    FLD_ZONE X + "AREA OF MINIMAL FLOOD HAZARD"  -> "X"
    FLD_ZONE X + any other or blank subtype
             (0.2 PCT ANNUAL CHANCE..., AREA WITH
             REDUCED FLOOD RISK DUE TO LEVEE, ...)  -> "X_shaded"
    FLD_ZONE B (legacy shaded X)                 -> "X_shaded"
    FLD_ZONE C (legacy unshaded X)               -> "X"
    D, OPEN WATER, AREA NOT INCLUDED, unknown,
    no polygon at the point, service failure     -> None, with a note

When the point touches several polygons (a zone edge), the worst one wins, and
an unknown polygon outranks X so an edge never resolves to X by default.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from ..config import ROOT
from ..models import FloodRisk, Listing

# --- operational constants (not finance assumptions) -----------------------

NFHL_QUERY_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
"""NFHL layer 28 is "Flood Hazard Zones" (S_FLD_HAZ_AR)."""
TIMEOUT_S = 10.0
CACHE_DIR = ROOT / "data" / "cache" / "flood"
CACHE_MAX_AGE_DAYS = 180          # effective FIRMs change rarely; refresh twice a year
COORD_DECIMALS = 5                # ~1 m; cache key precision
USER_AGENT = "home-search/0.1 (personal research script)"

# --- note texts (scoring and the digest match on these) ---------------------

NOTE_PRIOR_FLOODING = "Description mentions prior flooding; request the seller's disclosure."
NOTE_CLAIMS_NO_FLOODING = (
    "Description claims no flood history; FEMA zone does not confirm it, "
    "request the seller's disclosure."
)
NOTE_REMEDIATED = "Description mentions remediation; ask whether it followed flooding."
NOTE_ELEVATED = "Description says the house is elevated; ask for the elevation certificate."
NOTE_NO_COORDS = "No coordinates for this listing; FEMA flood zone not checked."
NOTE_NO_POLYGON = "No FEMA flood hazard polygon at this point; zone unknown."
NOTE_LOOKUP_FAILED = "FEMA flood zone lookup failed"   # followed by ": <reason>"
NOTE_UNRECOGNIZED = "Unrecognized FEMA flood zone"     # followed by ": <zone/subtype>"

# --- mapping -----------------------------------------------------------------

_SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
_SFHA_NUMBERED = re.compile(r"^[AV]\d{1,2}$")       # legacy A1-A30, V1-V30
_MINIMAL_SUBTYPE = "AREA OF MINIMAL FLOOD HAZARD"
_FLOODWAY_MARKERS = ("FLOODWAY", "ENCROACHMENT")

_SEVERITY = {"X": 0, "X_shaded": 1, None: 2, "AE": 3, "floodway": 4}


def _norm(value: object) -> str:
    return str(value).strip().upper() if value is not None else ""


def classify_feature(attrs: dict) -> tuple[str | None, str | None]:
    """Map one NFHL feature's attributes to (our zone, note-or-None)."""
    zone = _norm(attrs.get("FLD_ZONE"))
    subtype = _norm(attrs.get("ZONE_SUBTY"))
    sfha = _norm(attrs.get("SFHA_TF")) == "T"

    if any(marker in subtype for marker in _FLOODWAY_MARKERS):
        return "floodway", None
    if zone in _SFHA_ZONES or _SFHA_NUMBERED.match(zone) or sfha:
        return "AE", None
    if zone == "X":
        if subtype == _MINIMAL_SUBTYPE:
            return "X", None
        if not subtype:
            return "X_shaded", "FEMA zone X with no subtype; treated as shaded X."
        return "X_shaded", None
    if zone == "B":
        return "X_shaded", None
    if zone == "C":
        return "X", None
    label = zone or "blank"
    if subtype:
        label += f" / {subtype}"
    return None, f"{NOTE_UNRECOGNIZED}: {label}."


def classify_response(payload: dict) -> FloodRisk:
    """Turn an NFHL query response (f=json) into a FloodRisk. Worst polygon wins."""
    if "error" in payload:
        err = payload.get("error") or {}
        reason = (err.get("message") or "service error") if isinstance(err, dict) else str(err)
        return FloodRisk(notes=[f"{NOTE_LOOKUP_FAILED}: {reason}."])
    features = payload.get("features") or []
    if not features:
        return FloodRisk(notes=[NOTE_NO_POLYGON])

    worst: str | None = "X"
    notes: list[str] = []
    for feature in features:
        zone, note = classify_feature(feature.get("attributes") or {})
        if note and note not in notes:
            notes.append(note)
        if _SEVERITY[zone] > _SEVERITY[worst]:
            worst = zone
    return FloodRisk(fema_zone=worst, in_floodway=worst == "floodway", notes=notes)


# --- description keywords ----------------------------------------------------

_NEGATED_FLOOD = re.compile(
    r"\b(?:never|not|no|didn'?t|did\s+not|has\s+not|hasn'?t|have\s+not|haven'?t)"
    r"\s+(?:been\s+|ever\s+|any\s+)*(?:flooded|flooding|flood(?:\s+damage)?|took\s+(?:on\s+)?water)\b",
    re.IGNORECASE,
)
_PRIOR_FLOOD = re.compile(
    r"\b(?:flooded|flood\s+damage|took\s+(?:on\s+)?water)\b", re.IGNORECASE
)
_REMEDIATED = re.compile(r"\bremediat(?:ed|ion)\b", re.IGNORECASE)
_ELEVATED = re.compile(r"\b(?:elevated|raised\s+(?:above|out\s+of))\b", re.IGNORECASE)


def description_notes(description: str | None) -> list[str]:
    """Notes from listing text. "Never flooded" is reported as a claim, not as flooding."""
    if not description:
        return []
    notes: list[str] = []
    stripped = _NEGATED_FLOOD.sub(" ", description)
    if _PRIOR_FLOOD.search(stripped):
        notes.append(NOTE_PRIOR_FLOODING)
    if _NEGATED_FLOOD.search(description):
        notes.append(NOTE_CLAIMS_NO_FLOODING)
    if _REMEDIATED.search(description):
        notes.append(NOTE_REMEDIATED)
    if _ELEVATED.search(description):
        notes.append(NOTE_ELEVATED)
    return notes


# --- FEMA query with on-disk cache ---------------------------------------------


def _cache_path(lat: float, lon: float, cache_dir: Path) -> Path:
    return cache_dir / f"nfhl_{lat:.{COORD_DECIMALS}f}_{lon:.{COORD_DECIMALS}f}.json"


def _read_cache(path: Path) -> dict | None:
    try:
        if time.time() - path.stat().st_mtime > CACHE_MAX_AGE_DAYS * 86400:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def query_params(lat: float, lon: float) -> dict[str, str]:
    return {
        "geometry": f"{lon:.{COORD_DECIMALS}f},{lat:.{COORD_DECIMALS}f}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,DFIRM_ID",
        "returnGeometry": "false",
        "f": "json",
    }


def fetch_nfhl(
    lat: float,
    lon: float,
    *,
    cache_dir: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Query NFHL at a point, using the cache. Raises httpx.HTTPError, ValueError or TypeError.

    Only well-formed, error-free responses are cached, so a failure is retried
    on the next run (once; there is no retry loop within a run).
    """
    cache_dir = cache_dir or CACHE_DIR
    path = _cache_path(lat, lon, cache_dir)
    cached = _read_cache(path)
    if cached is not None:
        return cached

    with httpx.Client(
        timeout=TIMEOUT_S, transport=transport, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = client.get(NFHL_QUERY_URL, params=query_params(lat, lon))
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, dict):
        raise TypeError("unexpected response shape")

    if "error" not in payload and "features" in payload:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
        except OSError:
            pass
    return payload


def assess(listing: Listing) -> FloodRisk:
    """FEMA zone at the listing's point plus description notes. Never raises on lookup failure."""
    text_notes = description_notes(listing.description)
    if listing.lat is None or listing.lon is None:
        return FloodRisk(notes=[NOTE_NO_COORDS, *text_notes])
    try:
        payload = fetch_nfhl(listing.lat, listing.lon)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        reason = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            reason = f"HTTP {exc.response.status_code}"
        return FloodRisk(notes=[f"{NOTE_LOOKUP_FAILED}: {reason}.", *text_notes])
    risk = classify_response(payload)
    risk.notes = [*risk.notes, *text_notes]
    return risk
