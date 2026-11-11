# Home-search

A scheduled search for a Houston home to buy in 2027, budget $800k–$1.2M, in
**Bellaire** and **Spring Branch / Memorial**, limited to homes zoned to
Bellaire High School or to top SBISD schools (Memorial High School first).
It also tracks rentals and tells you, with the numbers, whether buying or
renting is the better financial move, then emails you a weekly digest plus an
immediate alert for standout listings.

**Status: scaffold.** Structure, configuration, data contracts and
specifications are in place; the pipeline stages are stubs to be implemented
package by package from [`docs/TASKS.md`](docs/TASKS.md).

## What it will do

1. **Collect** for-sale and rental listings from HAR (saved-search alert emails
   and its public results pages), with Redfin's CSV export as a second source.
2. **Verify school zoning** against district attendance-boundary polygons, so a
   listing's own (often wrong) school claims are never trusted.
3. **Check flood risk** from FEMA flood maps and listing text.
4. **Price each home** monthly under several loan scenarios, with Texas
   property tax, homestead exemption, Houston insurance, and jumbo thresholds.
5. **Decide rent vs. buy** per listing and overall: break-even year, net-worth
   difference at your horizon, and the assumption that would flip the answer.
6. **Score and rank**, then email a Sunday digest and hot alerts.
7. **Track the market** week by week per area, so the 2027 timing is informed.

## Layout

```
config/search.yaml           areas, zips, schools, criteria, scoring weights
config/assumptions.yaml      rates, taxes, insurance, growth (to be verified)
config/profile.example.yaml  template for your private finances
src/home_search/             sources → store → enrich → finance → scoring → digest
docs/                        ARCHITECTURE, FINANCE_MODEL, TASKS
.claude/agents/implementer   the Opus agent that builds each work package
```

## Try the scaffold

```
pip install -e ".[dev]"
pytest
home-search doctor
```

## Your private numbers

This repository is public. Your finances go in `config/profile.yaml`, which is
gitignored, and in a GitHub secret named `PROFILE_YAML` for scheduled runs.
They are never committed, logged, or stored in the database.

## Caveats

Output is a screening aid. Confirm zoning with the district, flood history with
the seller's disclosure, taxes with HCAD, and financing with a lender before
acting on any listing.
