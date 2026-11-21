---
name: discovery
description: "Find and assess new BIST or US equity ideas from dated business changes before portfolio sizing. Record the price hurdle, contrary evidence and a watch, pass or eligible verdict."
domain: equity
tags: [discovery, screening, research, equity]
requires_tools:
  - core_record_discovery
  - data_market_bist_screen
  - data_market_quotes
---

# Equity discovery

Find businesses worth researching, not trades. Do not use the user's current holdings to generate
or rank candidates, and do not propose weights. A good result can be no eligible names.

## 1. Set the universe and look for change

- For BIST, start with `data_market_bist_screen`. Use a liquidity or size range if the question
  calls for one, not an implicit large-cap cutoff. Follow `next_cursor` within the turn when
  more names could matter. Keep the same `capture_id`; report separate counts for separate
  captures, never sum overlapping universes. Match does not mean examined.
- For US equities, use the user's explicit list or a named, dated source list with disclosed
  membership. If neither is available, ask for it or state that US coverage is unavailable.
  Do not substitute daily movers or describe a supplied list as the whole US market.
  Pass its tickers as `us_universe`; report names with no usable evidence separately.
- Look for at least one of these paths to an opportunity, as the question warrants:
  - **Compounder** (`compounder`): repeat orders, better cash conversion or higher returns on
    new investment. Test working capital, dilution, or customer concentration.
  - **Cyclical turn** (`cyclical_turn`): inventory, utilization, pricing, contracted demand or
    newly funded capacity improves. Do not require positive trailing P/E. Check cash runway
    and use mid-cycle, not peak, economics.
  - **Supply chain** (`supply_chain`): connect a dated customer, regulatory or physical-market
    change to a supplier with material exposure. Verify contract terms, actual capacity and
    who keeps the margin. Commodity prices, inventories and positioning can test exposure;
    they are not the returns of an investable position.

## 2. Verify the economics and the price hurdle

- Compare the new event with a prior baseline. Confirm the business impact with primary
  evidence and express it in units, margin, cash flow or financing need. For BIST, separate
  nominal TRY growth from real growth, bank reporting from TMS 29, and FX revenue from net
  FX exposure. Missing analyst coverage is not adverse evidence. For the US, do not infer
  past consensus revisions from current estimates. Use optional PDF or IstFin tools only if
  already available in the session.
- Record a publication field on the evidence item that establishes the change, not its
  reporting period, file name, quote date, or the day Qua found it. The publication date must
  come from an evidence item also cited in `evidence`. Supported evidence paths include
  `filings[0].filed`, `disclosure.published_at`, `items[0].published_at` and
  `items[0].published` when `time_basis` is `published`. Use the correct item index.
  If unknown, omit `publication` and leave `early_claim` false. A source date alone does not
  prove the stock was underpriced or that Qua identified it at that time.
- Use `data_market_quotes` for the candidate's dated price on or after the publication date,
  then `calc` or a valuation tool to solve for one operating assumption. Chain the candidate's
  own price field (`prev:<id>.quotes.<SYMBOL>.price`) into the calculation with `prev:`. For
  example, derive market capitalization from price times shares, then divide by an assumed
  justified P/E to get required earnings. State the units, horizon and fixed assumptions.
  Compare against a plausible range grounded in filings or peers; don't use a trailing P/E
  hurdle for a loss-maker. If the inputs or a post-event quote are unavailable, leave the
  hurdle unknown.

## 3. Save the decisions and explain the shortlist

- Call `core_record_discovery` with `capture_id` or `us_universe` and the decisions already
  examined. For each, give the track, change, business impact, strongest contrary evidence,
  next observable milestone and exact session evidence IDs and paths. Record the actual
  rejected peer as a separate `pass` entry if examined. If no peer was examined, say so;
  unseen or unexamined names are not rejects.
- `eligible` means eligible for a separate portfolio review, not buy. It needs a dated
  publication and a calculated price hurdle. Use `watch` when the thesis looks credible but a
  material date, economic input or valuation is missing. Use `pass` when the evidence shows
  immaterial exposure, excessive risk, or a price hurdle the business is unlikely to meet.
- Show the matched, returned, unseen, examined, rejected, watch and eligible counts from the
  tool, separately for each universe. For a supplied US list, returned means membership was
  supplied, not that all prices or filings were fetched. Show each finalist's decisive fact,
  hurdle or gap, what would disprove it, and the saved evidence record ID. On reassessment,
  add a new verdict rather than treating the earlier one as if it never happened.
