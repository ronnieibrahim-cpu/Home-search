# Financial model

All parameters come from `config/assumptions.yaml` (public) and
`config/profile.yaml` (private). Nothing is hard-coded in the functions.

## 1. Monthly cost of ownership (`finance/costs.py`)

For a listing at price `P` under a loan scenario with down payment share `d`:

| Item | Formula |
|---|---|
| Loan | `L = P × (1 − d)` |
| Jumbo | `L > mortgage.conforming_limit` |
| Rate | `rate_30yr_fixed + scenario.rate_spread + (jumbo_rate_spread if jumbo)` |
| Principal and interest | `L × r / (1 − (1 + r)^−n)`, `r = rate / 12`, `n = term_years × 12` |
| Property tax | `(P − E × s) × rate_by_area[area] / 12`, see note |
| Homeowners insurance | `P × homeowners_pct_of_value / 12` |
| Flood insurance | `flood_annual_by_zone[zone] / 12`; unknown zone priced as `X_shaded` |
| PMI | `L × pmi_annual_pct / 12` if `scenario.pmi`, else 0 |
| HOA | listing value, else `hoa_monthly_default` |
| Maintenance | `P × maintenance_pct_of_value / 12` |

Property tax note: the homestead exemption `E` reduces only the school
district's share of the bill. `s` is the school district's fraction of the
total rate, which WP1 must add to `assumptions.yaml` per area
(`school_share_by_area`). Year-1 tax is computed on purchase price, never on
the seller's capped appraisal.

### Worked example (required test)

`P = 1,000,000`, scenario `conventional_20`, rate 6.50%, 30 years, area
`memorial` at 2.10%, school share 0.50, exemption 140,000, zone `X`, no HOA.

- Loan 800,000, conforming → P&I **5,056.54**
- Tax `(1,000,000 − 140,000 × 0.50) × 0.021 / 12` = **1,627.50**
- Homeowners `1,000,000 × 0.0085 / 12` = **708.33**
- Flood `900 / 12` = **75.00**
- Maintenance `1,000,000 × 0.01 / 12` = **833.33**
- Total **8,300.70** per month

The school share of 0.50 is a placeholder for the example only.

## 2. Affordability (`finance/affordability.py`)

Profile fields are named as in `config/profile.example.yaml`.

**The household's plan scenarios.** Besides the scenarios in `assumptions.yaml`,
build two from the profile, both with `financing.down_payment_pct` and no PMI:
`plan`, at the household's planning rate `financing.rate_assumption` (carried
as `rate_spread = rate_assumption − rate_30yr_fixed`, so it reconciles with the
household workbook), and `plan_market`, at today's market rate
(`rate_spread = 0`). When the profile sets no rate the two are identical and
only `plan` is built. They are listed first, `plan` then `plan_market`; the
flags below are evaluated on each scenario's own cost. The assumptions'
scenarios are always priced at the market rate; the profile's rate never
replaces `rate_30yr_fixed`. The physician-loan scenario is priced only when
`financing.physician_loan_programs` is set. Other profile values override
assumptions wherever both exist (closing cost share, modeled tax rate, price
band, bed and bath minimums).

**Cash.**
- `months = months from meta.generated to the target close date`
- `usable_cash = cash_for_purchase.balance + monthly_addition × months − keep_after_closing`
- `cash_needed = P × (down_payment_pct + closing_costs_pct)`
- `other_assets` are reported as months of reserves (`sum / total_monthly`), never added to cash.

**Two monthly figures, both shown.**
- `total_monthly`: the itemized figure from §1 (insurance, flood and maintenance from the listing's value and zone).
- `plan_basis_monthly`: `P&I + P × tax_rate / 12 + insurance_plus_maintenance_annual / 12`,
  the household workbook's convention, so digest numbers reconcile with it.
  Tax rate is the listing's actual rate when the source provides one, else
  `carrying_costs.property_tax_rate_modeled`.

Required test (synthetic profile, same convention): `P = 1,100,000`, 15% down,
6.5%, tax 2.1%, combined 15,000/yr → P&I **5,909.84**, plan basis **9,084.84**.

**Tests and flags.** Evaluated on the larger of the two monthly figures.
- `over_carry_target`: above the upper value of `total_housing_carry_target_monthly`.
- `over_hard_ceiling`: above `hard_ceiling_total_housing`. Disqualifies.
- `cash_short`: `cash_needed > usable_cash`; the blocker states the gap in dollars
  and the price at which it closes.
- `jumbo`: loan above the conforming limit (at 15% down, price above limit / 0.85).
- `dti_back_end = (total_monthly − maintenance + household.monthly_non_housing_debt) / income.gross_monthly_for_lender`.
- `within_comfort`: none of the above cost or cash flags raised.
- Rate bracket: the digest shows `plan` P&I at each end of `financing.rate_range`.

## 3. Rent vs buy (`finance/rent_vs_buy.py`)

Monthly simulation over `H = horizon_years`, two paths from identical starting cash.

**Buyer.** Pays down payment and buying costs at month 0. Each month pays
total ownership cost; tax, insurance and maintenance grow with home value at
`home_appreciation` (tax growth capped at `homestead_cap_annual`); P&I is fixed.
At `H`, sells: `equity = value_H × (1 − closing_cost_sell_pct) − loan_balance_H`.

**Renter.** Invests the buyer's month-0 outlay at `return_nominal`. Each month
pays rent (growing at `rent_growth`) plus renters insurance.

**Leveling.** Whichever path is cheaper in a month invests the difference at
`return_nominal`. This is what makes the comparison fair.

**Tax.** Buyer's benefit is `household.marginal_federal_rate × max(0, itemized − standard_deduction)`
where itemized = interest on debt up to `mortgage_interest_debt_cap`, plus
property tax up to `salt_cap`. Skip when no profile is loaded or the rate is unset, and say so.

**Result.**
`npv_buy_minus_rent = (buyer_net_worth_H − renter_net_worth_H) / (1 + inflation)^H`.

- `verdict`: `buy` if NPV > +2% of `P`, `rent` if < −2% of `P`, else `close_call`.
- `breakeven_year`: first year `y ≤ max(sensitivity.horizons)` where selling at
  `y` leaves the buyer ahead.
- `sensitivity`: NPV over the grid `horizons × appreciation × rate`.
- `rationale`: 2–4 sentences with the numbers, for example which monthly amount
  is unrecoverable on each side (interest + tax + insurance + maintenance vs.
  rent) and the break-even year.

## 4. What the digest must say as the advisor

The weekly summary answers, in this order:

1. **Buy or rent, today's market, your numbers**: the `portfolio_view` headline.
2. **What you can carry**: `max_price` per loan scenario against the 800k–1.2M band.
3. **Runway to 2027**: projected cash at purchase vs. cash needed for the median candidate.
4. **The swing factor**: the one assumption whose plausible range flips the verdict.

With a 10+ year horizon, buying normally wins in these zones on the
appreciation and forced-savings terms; the model's job is to catch the cases
where it does not: a listing priced far above its rent-equivalent, a flood-zone
insurance load, or a cash position that would drain the emergency fund.

## Limits to state in every digest footer

Estimates, not quotes. Insurance and tax figures must be replaced with real
quotes and the HCAD record before an offer. This tool does not replace a
lender pre-approval, an inspection, a seller's disclosure review, or a CPA.
