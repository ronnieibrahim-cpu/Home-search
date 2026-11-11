"""School zoning by point-in-polygon against district attendance boundaries. WP5.

Listing sites' school fields are frequently wrong at zone edges, and the whole
search hinges on zoning, so the boundary file is the only thing that can set
ZoningConfidence.VERIFIED. Boundary GeoJSON lives in data/boundaries/ (see the
README there for sourcing and the yearly refresh).
"""

from __future__ import annotations

from ..config import Area, SearchConfig
from ..models import Listing, SchoolZoning


def zone(listing: Listing, cfg: SearchConfig) -> SchoolZoning:
    """Resolve elementary / middle / high for a listing.

    No lat/lon, or no polygon hit -> confidence UNKNOWN (or LISTING_CLAIM when
    the description names a target school). Never guess from zip code.
    """
    raise NotImplementedError("WP5")


def is_target(zoning: SchoolZoning, area: Area, cfg: SearchConfig) -> bool:
    """True when the high school is a target and, per cfg.schools, verified."""
    raise NotImplementedError("WP5")
