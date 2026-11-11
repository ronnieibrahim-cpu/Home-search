# Architecture

## Flow

```
            ┌─ sources/har_web ─────┐
            ├─ sources/redfin_csv ──┤
schedule ──▶│                       ├─▶ store.upsert ─▶ enrich/schools ─▶ enrich/flood
            └─ sources/email_alerts ┘        │                                  │
                                         ChangeEvents                           ▼
                                             │            finance/costs ─▶ affordability ─▶ rent_vs_buy
                                             ▼                                  │
                                      digest/render ◀──────── scoring ◀─────────┘
                                             │
                                      digest/send ─▶ Gmail
```

Two entry points share the flow: `scan` (daily, emails only hot alerts) and
`digest` (weekly, also writes a `MarketSnapshot` per area and emails the digest).

## Decisions

- **Sources are plugins.** Anything implementing `sources.base.Source` can be
  added (a paid API later, for instance) without touching the pipeline. Sources
  fail independently and outages are reported in the digest.
- **HAR first.** HAR is the MLS's own site and the preferred source. It arrives
  three ways: saved-search alert emails (always on, runs anywhere), a browser
  task in the user's own signed-in Chrome (`docs/HAR_BROWSER_TASK.md`), and the
  public results pages (run from the user's computer). When sources disagree on a
  field, HAR wins. Redfin fills gaps and supplies rentals and coordinates.
- **Zoning is verified geometrically.** Only a polygon hit sets `VERIFIED`.
  Zip code and listing text never do.
- **Two config tiers.** `search.yaml` and `assumptions.yaml` are public.
  The profile is private and optional; without it the run still produces
  listings, zoning, flood and costs, and omits affordability and tax effects.
- **State.** SQLite. Local: `data/state.sqlite`. CI: restored from and pushed to
  an orphan `state` branch so history survives between runs. Listing data only.
- **Runs in both places.** GitHub Actions is the default scheduler. The same
  CLI runs locally for when a site blocks datacenter IPs. Both use the same
  database format; `docs/SETUP.md` (WP11) covers not running both at once.

## Scoring

Each part is 0–100; the score is the weighted sum using `scoring.weights`.

| Part | 100 | 0 |
|---|---|---|
| `schools` | verified, preferred high school, elementary on target list | verified target high only: 60; unverified claim: 30 |
| `value` | price per sqft ≥ 15% below the area's trailing 8-week median; +10 for a price cut, +5 if DOM > 45 | ≥ 15% above median |
| `flood` | zone X, no adverse notes | AE: 15; any "flooded" note caps the part at 40 |
| `fit` | meets or exceeds every `home` criterion | each miss scales down linearly; misses > 20% below a minimum score 0 for that criterion |
| `carrying_cost` | total monthly ≤ 80% of comfort level | ≥ 120% of comfort level; 50 when no profile |

Hard disqualifiers (listing dropped): floodway; flood zone beyond the profile's
`flood_tolerance`; non-target high school when `require_verified_high`;
excluded keyword; price above `stretch_max`.

Flags (shown as tags): `stretch`, `price_cut`, `new`, `back_on_market`,
`zoning_unverified`, `lot_value` (year built < 1975 and price per lot sqft near
the area's land value), `jumbo`, `flood_500yr`.

## Privacy rules (the repo is public)

1. `config/profile.yaml` is committed by the owner's choice. `PROFILE_YAML`, if
   set, still overrides it.
2. Credentials live only in `.env` or GitHub secrets.
3. Rendered emails go to SMTP or `out/` (gitignored) only.
4. No names, street address of the household, account numbers or email
   addresses anywhere in the repo; email fixtures are scrubbed.
