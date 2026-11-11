"""Flood risk. WP6.

Both target areas have real flood history (Brays Bayou through Bellaire;
Buffalo Bayou and the Addicks/Barker reservoir releases along Memorial), so
flood is a first-class score component and an insurance cost input, not a note.

Primary signal: FEMA National Flood Hazard Layer zone at the listing's lat/lon
(public ArcGIS REST query, cached per coordinate in data/cache/). Secondary:
keywords in the listing description ("never flooded", "flooded", "remediated",
"elevated"). A FEMA zone of X does not mean the house has not flooded; the
digest must say so and tell the reader to request the seller's disclosure.
"""

from __future__ import annotations

from ..models import FloodRisk, Listing


def assess(listing: Listing) -> FloodRisk:
    raise NotImplementedError("WP6")
