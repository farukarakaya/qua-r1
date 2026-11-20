---
name: bist-portfolio
description: "Construct and size a Turkish equity portfolio. Screen the full BIST, verify fundamentals and dividends, then compare USD drawdowns, correlations and risk budgets against BIST 100."
domain: portfolio
tags: [bist, turkey, portfolio, fx, usd, screening]
requires_tools:
  - data_market_bist_screen
  - data_market_bist
  - data_market_aligned_prices
  - data_market_quotes
  - compute_risk_correlation
  - compute_risk_drawdown
  - compute_portfolio_decomposition
  - compute_portfolio_performance
  - compute_portfolio_optimization
---

# BIST portfolio construction

1. **Scope the mandate.** Establish USD vs TRY objective, the BIST sleeve's share of total
   wealth, horizon, tolerable drawdown, and dividend vs growth preference. Ask about inputs
   that would change the sizing; otherwise state assumptions. Report USD portfolio risk even
   if the investor also tracks TRY.
2. **Screen before selecting.** Run `data_market_bist_screen` with no index restriction first
   so mid-caps compete with BIST 30 names. Apply a 3-month USD liquidity floor sized to the
   mandate, then quality, valuation or dividend criteria. Set a positive P/E floor for a
   profitable-stock screen. Set a positive EV/EBITDA floor if using an EV/EBITDA ceiling for
   positive EBITDA firms. Show the cutoffs, coverage and reasons for passing over candidates.
3. **Verify the shortlist.** If `search_docs` is available, search for candidates and metrics,
   then `read_doc` and cite the relevant pages. Verify dates and primary figures with
   `data_market_bist` financial statements and available IstFin KAP disclosures and ratios.
   Use `data_market_quotes` for fresh price and FX context, not for backtests. Use web search
   for gaps, with 3 to 6 public keywords per query (e.g. `LOGO KAP net döviz pozisyonu`); add
   dates only when needed. Distinguish reported numbers from forecasts and targets.
4. **Compute USD risk before sizing.** Use `data_market_aligned_prices` for all candidates,
   `USDTRY=X` and `XIST:XU100` in one request (3 to 5 years of daily bars preferred; use weekly
   only on genuinely shared closes). Check the actual dates, bars dropped and price bases.
   Recent listings may shorten the common window; say so and rerun the whole basket when it
   changes. Matching dates are not simultaneous quotes.
   - Convert each stock and index level to USD by dividing its TRY series pointwise by
     `USDTRY=X` (TRY per USD). Use `calc` with the aligned `columns` as named `values`, not
     rounded prices or manually expanded expressions. For a basket ordered LOGO, ENKAI,
     USDTRY, XU100, an example `calc` call is:
     ```json
     {
       "values": {"logo": "prev:e1.columns[0]", "enkai": "prev:e1.columns[1]", "fx": "prev:e1.columns[2]", "xu100": "prev:e1.columns[3]"},
       "expression": "{\"XIST:LOGO\": logo / fx, \"XIST:ENKAI\": enkai / fx, \"XIST:XU100\": xu100 / fx, \"benchmark_returns\": returns(xu100 / fx)}"
     }
     ```
     Replace `e1` with the aligned price record. Use ticker-based variable names in `calc`
     (`e` and `pi` are reserved) and a flat result dictionary. Reference its `result` series keys
     in the compute calls. Never pass the FX pair, index or benchmark returns as holdings.
   - Call `compute_risk_correlation` with USD closes by holding; run `compute_risk_drawdown`
     on each USD price path (with the shared dates) and on the proposed portfolio's returns.
   - Run `compute_portfolio_optimization` with `method="risk_parity"` (or another justified
     long-only baseline) on the USD stock series. For proposed, equal-weight and risk-budgeted
     weights, use `compute_portfolio_decomposition` with the same prices and the aligned
     `periods_per_year`. Report proposed capital weights and component risk contributions.
   - Feed each decomposition's `returns` to `compute_portfolio_performance`, with the USD
     `benchmark_returns` and same `periods_per_year`. Use a consistent USD risk-free assumption
     and label it. Compare cumulative return, annualized return/volatility and max drawdown.
     Also report XU100 USD price performance on the same dates. These are fixed-weight
     historical estimates, not forecasts. If alignment or USD math fails, state the gap
     and do not present untested weights as quantitatively sized.
5. **Check Turkey-specific adjustments before publishing.**
   - **TMS 29 vs BRSA:** Separate nominal BRSA bank statements (`financial_group="UFRS"`)
     from industrial/retail statements that apply TMS 29. Check the actual reporting period
     and basis; do not rank their raw ROE or P/E together without explaining the gap.
   - **Dividends:** Call `data_market_bist` with `dataset="corporate_actions"` for high-yield
     candidates. Check payout coverage, ex-dates, capital changes and recurring vs special
     payments. borsapy closes are split-adjusted but exclude cash dividends; label stock and
     benchmark comparisons as price returns, not total returns. Do not add today's yield to a
     historical return series.
   - **FX and investor costs:** State material net FX positions, hard-currency revenue share
     or gaps. Verify tax with current GİB guidance for the investor's residence and account.
     For a Turkish resident individual, the usual starting points are 0% Turkish withholding
     on qualifying BIST share-sale gains and 15% withholding at cash dividend distribution;
     check exceptions, filing requirements and treaty treatment. State any assumed FX
     conversion spread and commissions separately. Don't present gross estimates as after-tax.

Close with the measured USD portfolio and benchmark window, illustrative weights and dollar
sizes (use `calc`), concentration and downside tradeoffs, and evidence that would change sizing.
