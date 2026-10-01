# Weekly portfolio review — prompt

Used by both paths, so editing this file changes both at once:

- `/weekly-review` in Claude Code on the laptop (fetches the JSON itself)
- pasted by hand alongside `weekly_input.json` downloaded from the panel at
  `/fidata/weekly_input.json`

---

You are reviewing a personal investment portfolio. The attached
`weekly_input.json` is the complete set of facts available: totals and MPT
metrics, sector allocation, the 25 largest holdings (weight, return,
3-month/1-year gain, annualized vol, beta, cost-basis source), the best and
worst 3-month movers, unrealized losses, closed positions, annual capital
activity, this week's news, and upcoming earnings.

Write five sections, in this order, using exactly these headers on their own
line: `## Rebalancing`, `## Sector Drift`, `## Dead Money`,
`## Tax Planning`, `## Watch List`. Under each, 2–4 short paragraphs or
bullets.

`## Dead Money` uses `laggards`: five-year price return against SPY and
against the position's own sector ETF. `behind_both: true` is the strong case —
the sector can't be blamed. This is deliberately independent of cost basis: a
position can be up 70% on what was paid for it and still have gone nowhere for
five years while the market compounded. Name the capital tied up, not just the
percentage.

`## Tax Planning` uses `gain_harvest`, which ladders long-term taxable
positions worst-performer-first up to a realized-gain budget (the 0% long-term
bracket in a low-income year). Report `gain_realized`, `capital_freed` and
`headroom`. If `dominated_by` is present, lead with it — one position eating
most of the budget is the decision, and `without_it` shows what the rest of
the list achieves alone. Positions in `excluded` with term `mixed` were bought
within the last year and carry a short-term slice; say so rather than dropping
them silently. Always note that the budget applies to taxable income
*including* these gains.

`gain_harvest.tax_cost` prices the whole bill on the realized gain —
`total_tax`, `effective_pct`, and the `federal` / `state` breakdowns. Report
the total, not the federal half: California has no preferential capital-gains
rate, so the 0% federal bracket never makes a sale free. Inside `federal`,
`niit` is the IRC §1411 3.8% surtax, which runs on MAGI over $200,000 (single)
and so bites long before the 20% bracket's $545,500 of taxable income.
`state.federal_0pct_capacity` is the gain that actually fits in the federal 0%
bracket given that year's other income — when it exceeds `budget`, say so: the
budget is the user's stated rule of thumb, not the true ceiling, and the gap is
unused capacity.

Two things `tax_cost` does NOT model, both capable of exceeding the tax it does
compute. Say so whenever the plan's gain is material:

- **ACA premium tax credits.** Realized gain raises MAGI. If the 400%-of-FPL
  cliff applies, crossing it repays the full year's advance credits with no cap
  — thousands of dollars on the crossing dollar. This is the one input that can
  invert the recommendation.
- **An existing capital-loss carryforward.** IRC §1212(b) use is mandatory, so
  a carryforward nets against the harvested gain first and is consumed at 0% —
  which makes the harvest net-negative.

Never run a gain harvest and `harvest_candidates` in the same tax year without
flagging the conflict: §1222 nets losses against gains *before* the 0% bracket
is reached, so harvesting losses in a 0% year wastes them.

Rules:

- **Ground every number in the JSON. Invent nothing.** If something isn't in
  the data, say so rather than estimating it.
- **Tax-Loss Harvesting may only use `harvest_candidates`.** That list is
  already restricted to taxable accounts and already uses the broker's own
  cost basis. Roughly half the book sits in IRA/Roth/rollover accounts where a
  realized loss does nothing; `holdings[].return_pct` spans all accounts and
  is reconstructed from transaction history, so it is the wrong input for any
  tax decision and has been wrong by hundreds of percent on split-affected
  names. If `harvest_candidates` is empty, say there is nothing worth
  harvesting — do not go looking for losses in `holdings` or
  `unrealized_losses`.
- `taxable_positions` is the taxable-account view (broker basis) if you need
  more than the candidates; `accounts` gives each account's status.
- `cost_basis_source` on a holding: `default_cutoff` means the pipeline had no
  traced purchase, so that `return_pct` is a placeholder — flag it, never
  build a recommendation on it.
- Percentages in the bundle are already percentages (`return_pct: 12.5`
  means +12.5%); `market_value` is dollars.
- `holdings` lists only the largest positions — `holdings_note` says how many
  exist in total. Don't claim the list is the whole portfolio.
- Be specific and quantitative. "Trim SPY" is useless; "SPY is 14.7% of the
  book and overlaps AAPL/MSFT/AMZN held directly" is useful.
- No disclaimers about not being financial advice. No preamble.

Then publish the result as an Artifact page — a single self-contained HTML
file containing:

1. A header strip of the key numbers: market value, total P/L, ROIC, beta,
   Sharpe, effective-N, cash.
2. The four review sections, properly typeset (this is the main reason the
   page exists — the panel renders the same text as raw markdown).
3. A holdings table (largest positions: symbol, weight, return, 3-month,
   vol, beta, sector) that scrolls on a phone.
4. This week's news: position stories and market stories with their
   "why it matters" lines.
5. Upcoming earnings.
6. A footer with the bundle's `generated_at` timestamp.

Load the `artifact-design` skill before writing the page. Keep the file path
stable across weeks so republishing updates the same URL rather than creating
a new artifact each time.
