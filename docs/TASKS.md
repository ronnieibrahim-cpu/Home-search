# Work packages

Each package is one implementer session: one branch commit series, its own
tests, no edits outside the listed files unless the package says so. Signatures
in the stubs are the contract; change one only by flagging it to the
orchestrator first. Remove the `NotImplementedError` for what you implement.

Order: WP1 → WP2 → (WP3, WP4, WP5, WP6, WP7 in any order) → WP8 → WP9 → WP10 → WP11.

Status key: `[ ]` open, `[x]` done.

---

## [ ] WP1  Verify assumptions and school lists
Files: `config/assumptions.yaml`, `config/search.yaml`

Research only, no code. Confirm every value marked `VERIFY` against its
authority (FHFA, Freddie Mac PMMS, HCAD, Texas Comptroller, IRS, SBISD and HISD
feeder-pattern pages) and set `as_of`. For schools, confirm the
elementary → middle → high feeder patterns and correct the lists; a Memorial-area
elementary that feeds Stratford rather than Memorial HS must be labelled so.

Done when: no `VERIFY` comments remain, each changed value has its source URL in
a trailing comment, `pytest` passes.

## [ ] WP2  Store
Files: `store.py`, `tests/test_store.py`

Implement `normalize_key` and `Store` on stdlib `sqlite3`. Tables: `listings`
(current state, one row per key), `listing_sources` (key, source, url),
`history` (key, field, old, new, at), `alerts` (key, price, at), `snapshots`.

Done when tests cover: key normalization equivalences listed in the docstring;
new / price-change / status-change events; two sources merging to one row; the
two-miss rule in `mark_missing`; alert de-duplication at the same price and
re-alert after a price cut.

## [ ] WP3  Redfin CSV source
Files: `sources/redfin_csv.py`, `tests/test_redfin_csv.py`, `tests/fixtures/redfin_*.csv`

Follow the politeness rules in the module docstring exactly. Save one real CSV
per kind (sale, rent) as a fixture, with street numbers altered. `parse_csv`
is tested against fixtures; the live fetch gets one test marked `network`.

Done when: fixture parse yields correct `Listing` objects including lat/lon;
403/429/HTML-instead-of-CSV all raise `SourceUnavailable`; a zip returning zero
rows is distinguished from a block.

## [ ] WP4  Alert-email source
Files: `sources/email_alerts.py`, `sources/base.py` (`enabled_sources`),
`tests/test_email_alerts.py`, `tests/fixtures/*.eml`, `docs/SETUP.md` (saved-search section)

IMAP to Gmail with the app password. Track the last-seen UID per label in the
store's `meta` table (add it). Parsers must tolerate a template change by
returning `[]` and logging a warning that reaches the digest footer, not by
raising. Email addresses and names in fixtures must be scrubbed.

Done when: HAR and Redfin fixture emails parse to listings with address, price,
beds, baths, sqft and canonical URL; `enabled_sources` skips this source
cleanly when the secrets are absent.

## [ ] WP5  School zoning
Files: `enrich/schools.py`, `data/boundaries/*.geojson`, `tests/test_schools.py`

Source the boundary files per `data/boundaries/README.md`. Load polygons once
per process (shapely STRtree). Add ratings from a public source if one is
available without scraping; otherwise leave `ratings` empty and say so.

Done when tests cover, with real coordinates: an address inside each target
high-school zone; an address in a target zip but zoned to a non-target high
school; a listing with no coordinates → `UNKNOWN`; a description claim without
polygon match → `LISTING_CLAIM`.

## [ ] WP6  Flood
Files: `enrich/flood.py`, `tests/test_flood.py`

FEMA NFHL query with on-disk cache and a 10 s timeout; on failure return
`fema_zone=None` with a note rather than raising. Map FEMA's zone/subtype
fields onto `X`, `X_shaded`, `AE`, `floodway`.

Done when: mapping is unit-tested from recorded JSON responses; keyword notes
are tested; one `network` test hits the live service.

## [ ] WP7  Costs and affordability
Files: `finance/costs.py`, `finance/affordability.py`, `tests/test_costs.py`, `tests/test_affordability.py`

Implement per `docs/FINANCE_MODEL.md` §1–2. The worked example there is a
required test case, to the dollar.

Done when: amortization matches a reference value; jumbo detection flips at the
conforming limit; homestead exemption applies to the school portion only; PMI
appears only when the scenario says so; `max_price` is monotone in income and
savings; tests use synthetic profiles only.

## [ ] WP8  Rent vs buy
Files: `finance/rent_vs_buy.py`, `tests/test_rent_vs_buy.py`

Implement per `docs/FINANCE_MODEL.md` §3–4.

Done when tests show: verdict flips to rent as horizon shrinks; higher
appreciation never lowers `npv_buy_minus_rent`; zero-appreciation, zero-return
case matches a hand-computed value; fallback rent is used and labelled when no
comps exist; `rationale` cites the actual numbers.

## [ ] WP9  Scoring and pipeline
Files: `scoring.py`, `pipeline.py`, `tests/test_scoring.py`, `tests/test_pipeline.py`

Scoring formulas are in `docs/ARCHITECTURE.md`. The pipeline test runs end to
end with fake sources and a temp database, with and without a profile.

Done when: disqualifiers drop listings; a source outage is reported, not fatal;
a hot alert is sent once per key-and-price; `home-search scan --dry-run` works
on a laptop with no secrets set.

## [ ] WP10  Digest and delivery
Files: `digest/render.py`, `digest/send.py`, `digest/templates/*`, `tests/test_digest.py`

Follow the section order in `render.py`. Plain-text part is mandatory.

Done when: a rendered sample digest (synthetic data) is produced by a test and
attached for the user to review; dry-run writes to `out/`; nothing
profile-derived reaches stdout or logs.

## [ ] WP11  Scheduling and setup guide
Files: `.github/workflows/search.yml`, `docs/SETUP.md`, `README.md`

Uncomment the schedule, implement the `state` branch restore/push steps, add a
failure notification step. Write local scheduling instructions (launchd and
cron). Walk the whole setup once on a clean clone.

Done when: a manual `workflow_dispatch` run completes and the user receives a
real digest.
