---
name: fetching-pit-financials
description: Fetches point-in-time financial-statement values from SEC EDGAR XBRL facts for a given issuer ticker and concept, automatically routing between balance-sheet snapshots (instant concepts like Assets, Liabilities, StockholdersEquity, CashAndCashEquivalentsAtCarryingValue) and income/cashflow flows (duration concepts like NetIncomeLoss, Revenues, OperatingIncomeLoss, NetCashProvidedByOperatingActivities). Duration concepts are aggregated as TTM (4 trailing quarters); instant concepts return the latest reported snapshot. The filing-date filter blocks look-ahead bias from later amendments (10-Q/A, 10-K/A). Returns a TTMMetric or a pandas DataFrame. Use when the user asks for point-in-time fundamentals, TTM revenue / TTM net income, balance-sheet snapshots as of a date, look-ahead-safe XBRL values, historical financial backtests, EPS / total assets / equity / debt by ticker, or "what was AAPL's net income known on 2020-06-30".
---

# Fetching Point-In-Time Financials

Pull a single XBRL fact value for an SEC-registered issuer as it was knowable on a specific date. The bundled Python module is thread-safe, memory-efficient, and safe to import (no network calls until invoked).

The router does the right thing per statement type:
- **Balance-sheet line** (`instant` period_type, e.g. Assets) → latest reported snapshot whose `period_end <= as_of`.
- **Income / cashflow line** (`duration` period_type, e.g. NetIncomeLoss) → trailing twelve months ending on or before `as_of`.

Both paths apply the same point-in-time filter (`filing_date <= as_of`), so amendments filed after `as_of` cannot leak backward into the result.

## Contents
- When to use this skill
- Requirements
- Quick start (CLI + Python)
- Concept types: instant vs duration
- TTM correctness (span filter, FY-aware relabeling, fallback chain, has_gaps rejection)
- CIK override (delisted / share-class issuers)
- Look-ahead-bias guarantees
- Workflow
- Output schema
- Multi-ticker batch helpers (parallel fetch)
- Performance & thread safety
- Composing with other skills
- Parameters reference
- Troubleshooting
- Files

## When to use this skill

Trigger when the user mentions any of:
- Point-in-time (PIT) fundamentals, "as-of" financial values, look-ahead-safe joins
- TTM revenue, TTM net income, TTM cash flow, trailing-twelve-month metrics
- Balance-sheet line items: Assets, Liabilities, StockholdersEquity, LongTermDebt, CashAndCashEquivalentsAtCarryingValue
- Income-statement / cashflow line items: NetIncomeLoss, Revenues, OperatingIncomeLoss, NetCashProvidedByOperatingActivities, CapitalExpenditures
- "What was AAPL's net income known on 2020-06-30?", "MSFT total assets in 2023 Q3"
- Historical fundamental backtests; evaluating screens at a past date
- XBRL facts by US-GAAP or IFRS concept name

Do **not** trigger for:
- Filing release dates only (use `fetching-financial-notices` — return is just dates)
- Fund / ETF holdings (use `fetching-fund-universe`)
- Industry / sector classification (use `classifying-industry`)
- Live equity prices, options data, or non-XBRL filings (8-K, Form 4)

## Requirements

```bash
pip install -r requirements.txt
```

SEC mandates a contact email on every request. Supply it via one of:
- `--identity me@example.com` flag on the CLI
- `EDGAR_IDENTITY=me@example.com` environment variable
- `set_identity("me@example.com")` in Python, before the first call

## Quick start

### CLI

```bash
# Single PIT lookup — duration concept (TTM net income known as of 2024-06-30)
python scripts/pit_financials.py AAPL NetIncomeLoss --as-of 2024-06-30

# Single PIT lookup — instant concept (Total Assets reported as of 2024-06-30)
python scripts/pit_financials.py AAPL Assets --as-of 2024-06-30

# Time series — duration concept (one row per 10-Q / 10-K filing date)
python scripts/pit_financials.py AAPL NetIncomeLoss --start 2018-01-01 --end 2025-12-31 --out aapl_ni.csv

# Time series — instant concept, with audit frame
python scripts/pit_financials.py AAPL Assets --start 2018-01-01 \
    --out aapl_assets.csv --audit-out aapl_assets_audit.csv

# Multi-ticker — parallel fetch, long-format frame
python scripts/pit_financials.py AAPL MSFT NVDA GOOGL Assets --as-of 2024-06-30
python scripts/pit_financials.py AAPL MSFT NVDA Revenues --start 2020-01-01 --workers 8 --out rev.parquet

# Via environment variable
EDGAR_IDENTITY=me@example.com python scripts/pit_financials.py MSFT Revenues --as-of 2023-12-31
```

### Python

```python
from scripts.pit_financials import (
    get_pit_value, get_pit_value_batch,
    get_pit_series, get_pit_series_batch,
    set_identity,
)

set_identity("me@example.com")

# Duration concept → TTM aggregate from 4 source quarters
ni = get_pit_value("AAPL", "NetIncomeLoss", as_of="2024-06-30")
print(ni.value, ni.unit, ni.as_of_date, len(ni.period_facts))   # ~$100B USD, 4 facts

# Instant concept → single latest snapshot
a = get_pit_value("AAPL", "Assets", as_of="2024-06-30")
print(a.value, a.unit, a.as_of_date, len(a.period_facts))       # ~$337B USD, 1 fact

# Time series — both concept types use the same call
ts = get_pit_series("AAPL", "Assets", start="2023-01-01")
ts2, audit = get_pit_series("AAPL", "NetIncomeLoss", start="2023-01-01", with_audit=True)

# Multi-ticker — parallel network fetch, one row per ticker
snap = get_pit_value_batch(["AAPL","MSFT","NVDA","GOOGL"], "Assets", as_of="2024-06-30")

# Multi-ticker series — long-format frame with `ticker` column
long = get_pit_series_batch(["AAPL","MSFT"], "NetIncomeLoss", start="2020-01-01", end="2024-12-31")
```

## Concept types: instant vs duration

The skill auto-routes by the concept's XBRL `period_type`. Knowing which bucket a concept falls into is the most important mental model when using this skill.

### Instant concepts (balance-sheet snapshots)

Stock measurements at a single moment. Aggregating over time would be nonsensical (you don't sum quarterly cash balances).

| Statement | Common US-GAAP concepts |
|---|---|
| Balance sheet — assets | `Assets`, `AssetsCurrent`, `CashAndCashEquivalentsAtCarryingValue`, `Inventory`, `PropertyPlantAndEquipmentNet`, `Goodwill` |
| Balance sheet — liabilities | `Liabilities`, `LiabilitiesCurrent`, `LongTermDebt`, `LongTermDebtNoncurrent`, `AccountsPayableCurrent` |
| Balance sheet — equity | `StockholdersEquity`, `RetainedEarningsAccumulatedDeficit`, `CommonStockSharesOutstanding` |

**Routing behavior:** picks the latest fact whose `period_end <= as_of`. Tie-break on latest `filing_date` so a 10-Q/A or 10-K/A supersedes the original for the same `period_end`.

### Duration concepts (flows over a period)

Flow measurements over an interval. Quarterly facts are aggregated into a trailing-twelve-month sum so the figure is comparable across periods regardless of seasonality.

| Statement | Common US-GAAP concepts |
|---|---|
| Income statement | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `CostOfRevenue`, `GrossProfit`, `OperatingIncomeLoss`, `NetIncomeLoss`, `EarningsPerShareBasic`, `EarningsPerShareDiluted` |
| Cash-flow statement | `NetCashProvidedByOperatingActivities`, `NetCashProvidedByUsedInInvestingActivities`, `PaymentsToAcquirePropertyPlantAndEquipment` (capex) |

**Routing behavior:** delegates to `edgar.ttm.TTMCalculator`, which selects the 4 most recent consecutive quarters ending on or before `as_of` and sums them. Q4 is derived from FY minus Q1+Q2+Q3 when issuers don't file a discrete Q4 10-Q. The resulting `TTMMetric` exposes `has_calculated_q4` and `warning` fields when this happens.

### How to find the right concept name

If unsure, check edgar's discovery helpers:

```python
from edgar import Company
c = Company("AAPL")
# All concepts on AAPL's most recent income statement:
print(c.facts.get_statement("IncomeStatement").facts.head(40))
# Or grep the full fact list:
print({f.concept for f in c.facts._facts if "Revenue" in f.concept})
```

Bare names (e.g. `"Assets"`) are tried as raw, `us-gaap:`, and `ifrs-full:` prefixes; pass a fully-qualified name (e.g. `"us-gaap:Assets"`) to skip the fallback.

## TTM correctness

The default `edgar.ttm.TTMCalculator` works for the easy cases but misbehaves on three failure modes seen in real EDGAR data: mistagged comparative-period rows in 10-Qs, missing Q4 / YTD9M facts, and assembled TTM windows whose four "consecutive" quarters actually span a calendar gap. The skill layers four fixes on top of the calculator to produce a TTM that is either correct or explicitly refused.

### 1. Period-span consistency filter

10-Q filings routinely re-tag *comparative* prior-year values using the **filing's** `fiscal_period` label rather than the **fact's** period label. The pathological shape is something like `fiscal_period="Q1"` with `period_start="2022-01-01"` / `period_end="2022-12-31"` — a 365-day span labelled as a single quarter. These rows are usually segment / product-line breakouts, not the consolidated total, and feeding them to `TTMCalculator` produces a synthesised "Q4" computed as `WrongFY − YTDQ3` that can come out massively negative and wreck the trailing sum (e.g. CHD 2023-Q2 TTM revenue $311M instead of $5,375M; AXON 2024-Q2 TTM $461M instead of $1,672M).

Any fact whose declared `fiscal_period` is internally inconsistent with its `period_end - period_start` span is dropped:
- `FY` → 340–380 days
- `Q1` → 70–100 days
- `Q2` → 70–100 (single Q) or 160–200 (H1 YTD)
- `Q3` → 70–100 or 250–290 (9M YTD)
- `Q4` → 70–100 or 340–380 (FY YTD)
- Other labels (M3, off-cycle transitions) — left alone

### 2. FY-aware relabeling

After the span filter, each surviving fact's `(fiscal_year, fiscal_period)` is checked against its `period_end` and the issuer's inferred FY-end month (modal month across FY-tagged facts). When the labels don't match the period (typical for comparative columns whose period_end is in a *different* fiscal year than the filing), the fact is rewritten with canonical labels. This keeps the underlying data available to TTMCalculator under the correct FY/FP — without relabeling, those rows would either be discarded outright or mis-attributed to the current year and double-count Q1 (e.g. CBRE's prior-year Q1 column in a 2020 Q1 10-Q).

Re-tagging happens via `dataclasses.replace`; the original fact list is left intact. Off-cycle / mid-year transition periods that don't slot cleanly into a quarter or YTD shape are left with their original labels.

### 3. Fallback chain (strict algebraic identities)

When `TTMCalculator.calculate_ttm` returns `has_gaps=True`, three fallbacks are tried in order. Each is a strict identity (no heuristic interpolation, no look-ahead) and operates on the same pre-filtered, re-labeled fact set, so the PIT guarantee carries through every path.

1. **Synthesised Q4 = FY − (Q1 + Q2 + Q3).** When FY plus three single quarters of the same fiscal year are available but TTMCalculator's own `Q4 = FY − YTD9M` path failed (e.g. issuer doesn't tag YTD9M for this concept), build the missing Q4 fact algebraically. The synthesised Q4 carries `filing_date = max(filing_dates of its 4 inputs)` so it only becomes "knowable" once all four constituent filings are public. TTMCalculator is re-run on the augmented fact list.
2. **FY-anchor.** When `as_of` falls within ±30 days of an FY's `period_end` and that FY fact is in the candidate set, return it as the TTM directly. The TTM ending at FY-end *is* the FY value by definition. Tie-breaking on `filing_date` makes 10-K/A amendments supersede the original.
3. **YTD-current + (FY-prior − YTD-prior-same-position).** When neither single quarters nor YTD9M facts give a clean window but the issuer reports cumulative YTDs, decompose: e.g. `TTM(mid-Q3-2025) = YTD6M-2025 + (FY-2024 − YTD6M-2024)` = first 6 months of 2025 plus last 6 months of 2024. Useful for banks, insurers, and some utilities. Walks YTD9M → YTD6M → YTD3M and uses the latest fiscal year whose YTD-current is `<= as_of`.

### 4. has_gaps rejection (post-condition)

If `has_gaps` is *still* `True` after every fallback, the call **raises `ValueError`** rather than returning a misleading number. `has_gaps=True` is the calculator's signal that its 4-quarter window has non-consecutive `period_ends` — which happens when:

- `_select_ttm_window` stitched non-consecutive quarters because no Q4 was derivable for the missing year (e.g. PWR OCF 2019-08-27 stitched Q2-2018, Q3-2018, Q1-2019, Q2-2019 with a 182-day gap, value collapsed to $4M).
- An interpolated quarter spans multiple years because FY was paired with a stale-year YTD (e.g. CBRE CostOfRevenue 2020-06-01 synthesised "Q4-2018" with span 2016-10-01..2018-12-31, doubling TTM to $43B).

Reported quarters and same-fiscal-year interpolated quarters with consecutive ends are still kept — this rejection only fires when the assembly itself spans a calendar gap. Same-year interpolation paths (Q4=FY−YTD9M, Q3=YTD9M−YTD6M, Q2=YTD6M−Q1) all produce `has_gaps=False` when the YTD operand is from the same fiscal year (e.g. CNP 2021-11-28 derives Q4-2020 as FY-2020 − YTD9M-2020 → consecutive ends 2020-12-31 / 2021-03-31 / 2021-06-30 / 2021-09-30 → kept, $5M post-Texas-freeze TTM is correct).

The error message includes the offending `period_ends` list so you can see exactly why the assembly failed.

### Net effect

For instant concepts, the candidate-selection logic is unchanged (latest `period_end <= as_of`, tie-break on latest `filing_date`). For duration concepts, you get a correct TTM, a correct fallback-derived TTM, or an explicit refusal — never silently wrong. If you previously received a number that looked off for issuers like CHD / AXON / CBRE / CNP / PWR, you should now either get the right number or a `ValueError` you can catch and route around.

## CIK override (delisted / share-class issuers)

EDGAR's ticker→CIK map is current-only: a ticker that's been delisted, retired, or that uses a share-class format EDGAR doesn't recognise (e.g. `BRKB` rather than `BRK-B`) won't resolve through `Company(ticker)`. The skill consults a `cik_overrides.yaml` file in the project root (next to the running script — i.e. **outside** the skill's bundled `scripts/` dir when used embedded in a project) and uses `Company(cik)` directly when the ticker is listed there:

```yaml
overrides:
  RTN: 1047122          # Raytheon (merged into RTX)
  ATVI: 718877          # Activision Blizzard (acquired by Microsoft)
  BFB:  14693           # Brown-Forman class B (correct format: BF-B)
  BRKB: 1067983         # Berkshire B (correct format: BRK-B)
```

When the file is absent, the skill silently falls back to the standard ticker lookup — so the skill's own `scripts/` works standalone without any override file. The `cik_resolver.py` helper in the project root builds and maintains this YAML. Add an override whenever a known-good ticker fails with `Company not found`.

## Look-ahead-bias guarantees

Every PIT lookup applies the same filter at the candidate-selection stage:

```python
filing_date <= as_of   # blocks future amendments and future filings entirely
```

Verified empirically against AAPL's 10-K/A filed 2010-01-25 (which restated FY2008 Assets from $39,572M to $36,171M):

| Lookup as_of | Returned value | Source form filed |
|---|---|---|
| 2010-01-24 | $39,572M (original) | 10-K (2009-10-27) |
| **2010-01-25** | **$36,171M (amended)** | **10-K/A (2010-01-25)** |

The cutover is sharp — a one-day shift across the amendment date flips the answer. Same guarantee holds for duration concepts (NetIncomeLoss restatement at the same accession). Splits are detected against the same PIT-filtered fact set, so future split adjustments cannot retroactively shrink historical values either.

This is the property the skill exists to provide — never compute a backtest signal from it without the filter.

## Workflow

Copy this checklist and tick items as you progress:

```
PIT Financial Fetch:
- [ ] Step 1: Confirm the issuer ticker
- [ ] Step 2: Confirm the XBRL concept and verify it exists on the issuer
- [ ] Step 3: Decide single PIT (--as-of) or time series (--start/--end)
- [ ] Step 4: Ensure an EDGAR identity is configured
- [ ] Step 5: Call get_pit_value or get_pit_series
- [ ] Step 6: Inspect the result (warnings, source-fact audit) before trusting it
- [ ] Step 7: Save or display
```

### Step 1 — Ticker

US-listed SEC-registered issuer (e.g. `"AAPL"`, `"MSFT"`, `"NVDA"`). Foreign issuers that file 20-F instead of 10-K are not supported.

### Step 2 — Concept

Use the bare US-GAAP / IFRS taxonomy name (e.g. `"NetIncomeLoss"`, `"Assets"`). The router prefixes it with `us-gaap:` and `ifrs-full:` automatically. If the concept doesn't exist for this issuer, the call raises `ValueError("No facts found for concept: ...")` — pick a different concept (see "How to find the right concept name" above).

### Step 3 — Single PIT or series

- Single PIT (`get_pit_value` / `--as-of`): one number for one date. Use for screens, ratios at a moment, or sanity checks.
- Series (`get_pit_series` / `--start` / `--end`): one row per filing date in the window. Use for historical backtests, charting, or feature engineering. Default observation cadence is "every 10-Q / 10-K filing date for this concept" — typically four per year.

### Step 4 — Identity

`set_identity` must be called once per process. If `EDGAR_IDENTITY` is set in the environment, use it; otherwise ask the user for an email.

### Step 5 — Fetch

```python
m = get_pit_value("AAPL", "Assets", as_of="2024-06-30")
ts = get_pit_series("AAPL", "Assets", start="2018-01-01", end="2025-12-31")
ts, audit = get_pit_series("AAPL", "Assets", with_audit=True)
```

### Step 6 — Inspect

Always check before trusting a duration value:
- `m.has_gaps` — should be `False` on any value that's actually returned. When TTMCalculator emits `has_gaps=True` and the three fallbacks (synthesised Q4 / FY-anchor / YTD+prior-tail) can't fix it, the call raises `ValueError` rather than handing back a misleading number. See the "TTM correctness" section. If you do see `has_gaps=True` in a returned value, treat it as approximate and inspect `period_facts`.
- `m.has_calculated_q4` — `True` if Q4 was derived from FY − (Q1+Q2+Q3). Almost universal for US issuers; not a red flag on its own.
- `m.warning` — human-readable note from the TTM calculator or one of the fallbacks (e.g. `"TTM via FY-anchor fallback ..."`, `"TTM via YTD+prior-tail fallback ..."`). Surface it to the user when present.
- `m.period_facts` — the source FinancialFact objects (4 for TTM, 1 for instant). Inspect `form_type` to see if any are amendments (`10-Q/A`, `10-K/A`).

For a series, the `audit_df` (when `with_audit=True`) flattens these source facts to one row per source — ideal for "which filing fed which observation" debugging.

### Step 7 — Output

Default: print the metric or `df.head()`. For persistence, prefer parquet (`df.to_parquet("out.parquet", index=False)`) over CSV for speed and dtype fidelity.

## Output schema

### `get_pit_value(ticker, concept, as_of)` → `edgar.ttm.TTMMetric`

| Attribute | Type | Notes |
|---|---|---|
| `value` | `float` | The point-in-time value. Currency-denominated unless `unit` says otherwise. |
| `unit` | `str` | `USD`, `shares`, `USD/shares`, etc. |
| `as_of_date` | `datetime.date` | For TTM: end of the TTM window. For instant: the snapshot's `period_end`. |
| `concept` | `str` | Resolved concept name (e.g. `us-gaap:Assets`). |
| `label` | `str` | Human-readable label from the taxonomy. |
| `periods` | `list[tuple[int, str]]` | TTM: 4 `(fiscal_year, fiscal_period)` tuples. Instant: 1 tuple. |
| `period_facts` | `list[FinancialFact]` | The source facts that produced `value`. |
| `has_gaps`, `has_calculated_q4`, `warning` | `bool, bool, str | None` | TTM-quality flags; always `False, False, None` for instant. |

### `get_pit_series(ticker, concept, start=None, end=None)` → `pd.DataFrame`

| Column | Type | Example |
|---|---|---|
| `observation_date` | `datetime.date` | `2024-08-02` (a 10-Q filing date) |
| `period_end` | `datetime.date` | TTM window end / instant snapshot date |
| `concept`, `label`, `unit` | `str` | metadata |
| `value` | `float` | the TTM aggregate or snapshot value |
| `periods` | list | source `(fy, fp)` tuples |
| `has_gaps`, `has_calculated_q4`, `warning` | `bool, bool, str | None` | quality flags |

With `with_audit=True`, also returns an `audit_df` with one row per source fact: `source_value`, `source_period_end`, `source_filing_date`, `source_form_type`, `source_accession`, etc. Use it to inspect which 10-Q / 10-K (or amendment) fed each observation.

## Multi-ticker batch helpers (parallel fetch)

For workloads that span many tickers (cross-sectional screens, fund-universe backtests), use the batch entry points instead of looping `get_pit_value` / `get_pit_series` yourself. They run the per-ticker EDGAR fetches concurrently via a `ThreadPoolExecutor` and return long-format frames.

```python
# Cross-sectional snapshot — one row per ticker
snap = get_pit_value_batch(
    ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN"],
    "Assets", as_of="2024-06-30",
    max_workers=8,
)
print(snap[["ticker", "as_of_date", "value", "unit"]].to_string(index=False))

# Multi-ticker time series — one row per (ticker, observation_date)
ts_long = get_pit_series_batch(
    ["AAPL", "MSFT", "NVDA"], "NetIncomeLoss",
    start="2020-01-01", end="2024-12-31",
    max_workers=8,
)
# Same call with audit
ts_long, audit_long = get_pit_series_batch(
    ["AAPL", "MSFT"], "NetIncomeLoss",
    start="2020-01-01", with_audit=True,
)
```

**Failure behaviour:** per-ticker errors are logged at DEBUG and the offending ticker is dropped from the result frame. The batch never raises on a single bad ticker, so it's safe to feed in a fund-universe ticker list where some symbols may not have XBRL coverage.

**Empty inputs** return an empty DataFrame with the correct columns — callers can `concat` without conditional checks.

## Performance & thread safety

**Speedup.** With the cold cache, parallel batch is ~2× faster than a sequential loop of `get_pit_value` for ~6 tickers. The cap is the GIL during XBRL parsing of each ticker's fact list (CPU-bound, ~1s per ticker), not network I/O — process-level parallelism would scale further but is rarely needed for screens.

| Workload (6 tickers, cold cache) | Sequential | Batch (workers=8) |
|---|---|---|
| `get_pit_value` × 6 (Assets) | ~6.0s | ~2.5s |
| `get_pit_series` × 4 (NetIncomeLoss, 2y) | ~3.6s | ~2.3s |

**Thread safety.** Every entry point is safe to call from multiple threads:

- `_fetcher` is a `functools.lru_cache` (thread-safe by design); concurrent first-time calls for the same ticker race on a class-level lock and the loser reads the winner's instance — verified empirically (16 threads × 200 tasks × 8 tickers, zero errors, all per-ticker values consistent).
- `FinancialFacts.facts` uses double-checked locking on a per-instance `RLock` for the lazy fact-fetch.
- The class-level `_EDGAR_LOCK` is held *only* across `Company(ticker)` construction (which can mutate edgar's shared global session). The heavier fact-list materialisation runs lock-free, which is why the batch parallelism actually pays off — earlier the lock spanned the network call and capped speedup at ~1.3×.

If you embed the batch helpers inside your own threads or async loop, no additional locking is needed.

## Composing with other skills

The four skills compose into a full PIT screening pipeline:

```python
# 1. fetching-fund-universe → constituents of a fund
from scripts.fund_holdings import get_fund_holdings, set_identity as _id1
_id1("me@example.com")
holdings = get_fund_holdings("QQQ", dates=["2024-12-31"])
tickers = holdings["ticker_resolved"].dropna().unique()

# 2. fetching-financial-notices → release calendar (so we know valid as_of cutoffs)
from scripts.financial_notices import get_financial_notices_batch, set_identity as _id2
_id2("me@example.com")
calendar = get_financial_notices_batch(tickers)

# 3. fetching-pit-financials → fundamentals as of a chosen date (parallel)
from scripts.pit_financials import get_pit_value_batch, set_identity as _id3
_id3("me@example.com")
asof = "2024-12-31"
ni  = get_pit_value_batch(tickers, "NetIncomeLoss", asof, max_workers=8)      # TTM
eq  = get_pit_value_batch(tickers, "StockholdersEquity", asof, max_workers=8) # snapshot
fundamentals = (ni[["ticker", "value"]].rename(columns={"value": "ttm_ni"})
                .merge(eq[["ticker", "value"]].rename(columns={"value": "equity"}),
                       on="ticker", how="outer"))

# 4. classifying-industry → sector overlay
from scripts.industry_classifications import get_industry_table, set_identity as _id4
_id4("me@example.com")
industries = get_industry_table(holdings)
```

This skill does not import the others; it just returns objects the others can consume.

## Parameters reference

`get_pit_value(ticker, concept, as_of, *, split_adjust=True)` → `TTMMetric`

| Parameter | Type | Notes |
|---|---|---|
| `ticker` | `str` | Non-empty. Whitespace stripped. Validated via decorator. |
| `concept` | `str` | Non-empty. Bare name auto-tries `us-gaap:` and `ifrs-full:` prefixes. |
| `as_of` | str / date / datetime / Timestamp | The PIT cutoff. Only facts with `filing_date <= as_of` are considered. |
| `split_adjust` | `bool` | Default `True`. Applies detected stock-split adjustments (matters for share-denominated concepts like EPS, shares outstanding). |

`get_pit_series(ticker, concept, *, start=None, end=None, split_adjust=True, with_audit=False)`

| Parameter | Type | Notes |
|---|---|---|
| `start`, `end` | date-like or `None` | Inclusive observation-date window on filing dates. `None` = no bound. |
| `with_audit` | `bool` | When `True`, returns `(ts_df, audit_df)`. When `False` (default), returns just `ts_df` so the API matches the other skills. |

`get_pit_value_batch(tickers, concept, as_of, *, split_adjust=True, max_workers=8)` — parallel multi-ticker `get_pit_value`.

| Parameter | Type | Notes |
|---|---|---|
| `tickers` | iterable of `str` | Deduplicated and whitespace-stripped. Empty / non-string entries dropped. Per-ticker failures logged at DEBUG and skipped. |
| `max_workers` | `int` | Parallel ticker fetches (default 8). Drop to 1-2 if you hit HTTP 429. |

Returns a `DataFrame` with columns `ticker`, `concept`, `label`, `as_of_date`, `value`, `unit`, `periods`, `has_gaps`, `has_calculated_q4`, `warning` — one row per successful ticker, sorted by `ticker`.

`get_pit_series_batch(tickers, concept, *, start=None, end=None, split_adjust=True, max_workers=8, with_audit=False)` — parallel multi-ticker `get_pit_series`.

| Parameter | Type | Notes |
|---|---|---|
| `tickers`, `max_workers` | as above | |
| `with_audit` | `bool` | When `True`, returns `(ts_long, audit_long)` — both with a `ticker` column prepended. When `False` (default), returns just `ts_long`. |

`FinancialFacts(ticker)` — the underlying class. Its `.facts` property does one EDGAR call and caches; `.pit_value(...)` and `.pit_series(...)` reuse the cache. Constructed transparently per ticker via an LRU cache in the functional façade.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `IdentityError` / missing identity | Call `set_identity("email")` or set `EDGAR_IDENTITY` before any call |
| `ValueError: No facts found for concept: X` | The issuer doesn't report concept `X` under the names tried. Verify with `Company(ticker).facts.get_statement(...)`. |
| `ValueError: No instant facts for X available as of D` | The issuer hadn't reported `X` yet by date `D`. Choose a later `as_of` or check the earliest-known date for the concept. |
| `ValueError: Insufficient quarters for TTM` | Fewer than 4 quarters of `X` are available by `as_of` (typically a young IPO). Try a later `as_of`. |
| `ValueError: ticker must be a non-empty str` | Pass the ticker as a non-empty string literal |
| `ValueError: concept must be a non-empty str` | Pass the concept as a non-empty string literal |
| TTM value looks ~4× the quarterly figure | That is correct — TTM sums 4 quarters. Use the `periods` field to confirm window. |
| TTM `warning` mentions calculated Q4 | Normal; US issuers report YTD not discrete Q4. Q4 = FY − YTD-through-Q3. |
| `has_gaps=True` | A quarter is missing from the TTM window. Treat the value as approximate. (Note: when has_gaps=True survives all three fallbacks, the call now raises `ValueError` instead of returning the value — see next row.) |
| `ValueError: TTM rejected: ... has_gaps=True ... non-consecutive period_ends` | TTMCalculator's 4-quarter window spans a calendar gap and the synthesised-Q4 / FY-anchor / YTD+prior-tail fallbacks all failed. The sum doesn't represent a real 12-month aggregate, so the skill refuses rather than return a wrong number. The error includes the offending `period_ends` list. Catch and route around (skip ticker, try a different `as_of`, or use a different concept). |
| Source `form_type` is `10-Q/A` or `10-K/A` | An amendment was the most recent fact known by `as_of`. This is by design — amendments supersede originals when they are publicly available. |
| HTTP 429 rate-limit | Sleep and retry; one ticker = one EDGAR call (cached afterward). |
| `ModuleNotFoundError: edgar` | `pip install -r requirements.txt` (package name is `edgartools`) |

## Files

- `scripts/pit_financials.py` — main module + CLI (execute or import)
- `examples.md` — copy-paste recipes for both instant and duration concepts
- `requirements.txt` — pinned dependencies
