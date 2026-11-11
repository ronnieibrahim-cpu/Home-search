# Attendance boundaries

GeoJSON polygons used by `enrich/schools.py` to verify zoning (WP5).

Expected files, one feature per school, with properties `school`, `level`
(`elementary` | `middle` | `high`) and `school_year`:

- `sbisd.geojson`: Spring Branch ISD attendance zones
- `hisd.geojson`: Houston ISD attendance zones (only schools near Bellaire needed)

Sourcing, in order of preference: the district's own published GIS layer or
school-finder map service; then the Texas Education Agency / NCES School
Attendance Boundary data. Record the source URL and download date at the top of
each file's `metadata` object.

Boundaries change. Districts usually publish next year's zones in spring;
refresh these files each April and before any offer is made. The digest prints
the `school_year` it used. A zoning result from this tool is a screen, not a
guarantee: confirm with the district's address lookup before committing to a house.
