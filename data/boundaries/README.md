# Attendance boundaries

GeoJSON polygons used by `enrich/schools.py` to verify zoning (WP5). Only a hit
in one of these polygons can mark a listing's zoning `VERIFIED`.

**Status:** not yet present. The build session's network blocked the district
and ArcGIS hosts, so the owner must fetch these files from an unblocked machine
using the steps below. Until then every listing is `UNKNOWN` or `LISTING_CLAIM`,
and `missing_boundaries()` reports the absent files for the digest footer.
`tests/fixtures/boundaries/` holds SYNTHETIC rectangles for tests only; never
copy those here.

## Expected files

One file per district, named after `district` in `config/search.yaml`, lower case:

- `sbisd.geojson`: Spring Branch ISD attendance zones
- `hisd.geojson`: Houston ISD attendance zones (the whole district is fine; at
  minimum Bellaire HS and every zone touching the Bellaire fringe zips)

Format:

- A GeoJSON `FeatureCollection` in **WGS84 lon/lat (EPSG:4326)**. Files in a
  projected system (e.g. Texas South Central, EPSG:2278, coordinates in the
  millions) are rejected as unusable.
- One feature per school zone, geometry `Polygon` or `MultiPolygon`.
- Properties on every feature:
  - `school`: the school's name. Spelling variants are fine ("MEMORIAL H S",
    "Bunker Hill El"); they are matched to the names in `search.yaml`.
  - `level`: `elementary`, `middle` or `high`. Include all three levels, but
    the high-school layer is the one that decides `VERIFIED`.
  - `school_year`: e.g. `"2026-2027"`. Printed in the digest.
- A top-level `metadata` object with `source` (URL) and `downloaded` (date).

## Where to get them

Preference order: the district's own layer (current year), then the federal
survey (older but uniform).

1. **District GIS / school-finder layers.** Both districts publish their zone
   maps through an Esri ArcGIS map viewer.
   - SBISD: the "Attendance Boundaries" / school-finder map linked from
     springbranchisd.com (Schools > Find My School / Attendance Zones).
   - HISD: the "School Boundary Maps" / "Zoned Schools" map linked from
     houstonisd.org (Find My School / Attendance Boundaries).
   Open the map, open the browser's developer tools Network tab, and look for
   requests to a `.../FeatureServer/<n>/query` or `.../MapServer/<n>/query`
   URL (one layer each for elementary, middle and high). For each layer,
   download it as GeoJSON:

   ```
   <layer URL>/query?where=1%3D1&outFields=*&outSR=4326&f=geojson
   ```

   (If a layer holds more than ~1000–2000 features add `&resultOffset=` paging;
   attendance layers are far smaller than that.) If the district publishes a
   shapefile or KMZ download instead, use that.

2. **NCES School Attendance Boundary Survey (SABS).** nces.ed.gov >
   Programs > EDGE > Geographic > School Attendance Boundaries. Download the
   most recent national shapefile (2015-16 is the last full release, so treat
   it as a fallback only and set `school_year` accordingly), then filter to
   the district by `leaid` (look up each district's 7-digit NCES district ID
   on the NCES "Search for Public School Districts" page). Check its level
   field's coding in the accompanying documentation and map it to
   `elementary` / `middle` / `high`.

## Converting to the expected properties

With GDAL (`ogr2ogr`) installed, for each downloaded layer (shown here for an
SBISD high-school layer whose name field is `SCHOOL_NAME`; check the real field
name with `ogrinfo -so <file> <layer>`):

```
ogr2ogr -f GeoJSON -t_srs EPSG:4326 sbisd_high.geojson <downloaded file> \
  -sql "SELECT SCHOOL_NAME AS school, 'high' AS level, '2026-2027' AS school_year FROM <layer>"
```

Repeat with `'middle'` and `'elementary'`, then merge the three into one
collection, e.g. in Python:

```python
import json, datetime
feats = []
for lvl in ("elementary", "middle", "high"):
    feats += json.load(open(f"sbisd_{lvl}.geojson"))["features"]
json.dump({"type": "FeatureCollection",
           "metadata": {"source": "<layer URL>", "downloaded": str(datetime.date.today())},
           "features": feats}, open("sbisd.geojson", "w"))
```

Without GDAL, the `f=geojson&outSR=4326` query above already gives WGS84
GeoJSON; rename the name property to `school` and add `level` and
`school_year` with a few lines of Python like the merge above.

## Check it

```
python3 -c "from home_search.config import load_search; \
from home_search.enrich.schools import missing_boundaries; \
print(missing_boundaries(load_search()))"
```

prints `[]` when both files load. Then spot-check a few known addresses (one
inside each target high school's zone, one just outside it) with the district's
own address lookup and add them to `tests/test_schools.py` as real-coordinate
cases. Do not use the household's own address.

## Refresh

Boundaries change. Districts usually publish next year's zones in spring;
refresh these files each April and before any offer is made. The digest prints
the `school_year` it used. A zoning result from this tool is a screen, not a
guarantee: confirm with the district's address lookup before committing to a house.
