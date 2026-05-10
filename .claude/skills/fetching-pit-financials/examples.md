# Examples — fetching-pit-financials

Copy-paste recipes for both **instant** (balance-sheet) and **duration** (income / cashflow) concepts. Every snippet assumes an identity has already been configured (via `set_identity(...)` or `$EDGAR_IDENTITY`).

## Contents
- Setup
- Single PIT lookup — duration concept (TTM)
- Single PIT lookup — instant concept (snapshot)
- Time series — instant concept (Total Assets quarterly history)
- Time series — duration concept (TTM Net Income with audit)
- Inspect TTM quality flags before trusting a value
- Detect amendments in the source facts
- Cross-sectional snapshot across many tickers (parallel)
- Multi-ticker time series (long-format frame)
- Iterate over many concepts at one date (one issuer)
- Build a TTM revenue × balance-sheet ratio (TTM Revenues / Equity)
- Embedding the batch helpers in your own threads
- Save to parquet / CSV
- Composing with the other skills

## Setup

```python
from scripts.pit_financials import (
    FinancialFacts,
    get_pit_value,
    get_pit_value_batch,
    get_pit_series,
    get_pit_series_batch,
    set_identity,
)

set_identity("me@example.com")
```

## Single PIT lookup — duration concept (TTM)

Duration concepts get aggregated to trailing-twelve-months. The result holds 4 source facts.

```python
ni = get_pit_value("AAPL", "NetIncomeLoss", as_of="2024-06-30")
print(f"{ni.label}: {ni.value:,.0f} {ni.unit}")
print(f"TTM window ends: {ni.as_of_date}")
print(f"Source quarters: {ni.periods}")
# Net Income (Loss) Attributable to Parent: 100,389,000,000 USD
# TTM window ends: 2024-03-30
# Source quarters: [(2023, 'Q3'), (2023, 'Q4'), (2024, 'Q1'), (2024, 'Q2')]
```

Other duration concepts to try: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `OperatingIncomeLoss`, `GrossProfit`, `EarningsPerShareBasic`, `NetCashProvidedByOperatingActivities`.

## Single PIT lookup — instant concept (snapshot)

Instant concepts return the latest reported value with a single source fact.

```python
a = get_pit_value("AAPL", "Assets", as_of="2024-06-30")
print(f"{a.label}: ${a.value:,.0f} {a.unit}")
print(f"Snapshot reported as of: {a.as_of_date}")
print(f"Source filing: {a.period_facts[0].form_type} on {a.period_facts[0].filing_date}")
# Assets: $337,411,000,000 USD
# Snapshot reported as of: 2024-03-30
# Source filing: 10-Q on 2024-05-03
```

Other instant concepts to try: `Liabilities`, `StockholdersEquity`, `LongTermDebt`, `LongTermDebtNoncurrent`, `CashAndCashEquivalentsAtCarryingValue`, `CommonStockSharesOutstanding`.

## Time series — instant concept (Total Assets quarterly history)

```python
ts = get_pit_series("AAPL", "Assets", start="2018-01-01", end="2025-12-31")
print(ts[["observation_date", "period_end", "value", "unit"]].tail())
# observation_date  period_end       value unit
#       2024-11-01  2024-09-28  3.6498e+11  USD
#       2025-01-31  2024-12-28  3.4408e+11  USD
#       2025-05-02  2025-03-29  3.3123e+11  USD
#       2025-08-01  2025-06-28  3.3149e+11  USD
#       2025-10-31  2025-09-27  3.5924e+11  USD
```

One row per filing date. `observation_date` is when the value first became knowable; `period_end` is the snapshot date inside the filing.

## Time series — duration concept (TTM Net Income with audit)

The audit frame flattens the source facts so you can see exactly which quarters fed each TTM observation.

```python
ts, audit = get_pit_series(
    "MSFT", "NetIncomeLoss",
    start="2023-01-01", end="2024-12-31",
    with_audit=True,
)

# Top of the time-series frame
print(ts[["observation_date", "period_end", "value", "warning"]].head())

# Source facts behind the FIRST observation
first_obs = ts["observation_date"].iloc[0]
print(audit[audit["observation_date"] == first_obs][[
    "source_period_end", "source_fiscal_year", "source_fiscal_period",
    "source_value", "source_form_type", "source_filing_date",
]])
```

## Inspect TTM quality flags before trusting a value

Always check these before charting or reporting a TTM number. The skill rejects irrecoverably-broken windows itself, so most of what surfaces here is informational:

```python
try:
    m = get_pit_value("NVDA", "Revenues", as_of="2024-12-31")
except ValueError as e:
    # Raised when TTMCalculator's window has has_gaps=True AND none of
    # the three fallbacks (synthesised Q4 / FY-anchor / YTD+prior-tail)
    # could fix it. The error message includes the offending period_ends.
    print(f"TTM unavailable for this (ticker, concept, as_of): {e}")
else:
    if m.has_gaps:
        # Rare — has_gaps=True usually triggers ValueError above. If you
        # see it here, the calculator emitted it but the fallbacks
        # produced a usable shape; treat with caution and inspect period_facts.
        print("WARNING: missing quarter in TTM window — value is incomplete")
    if m.has_calculated_q4:
        print("Q4 was derived from FY − (Q1+Q2+Q3) — normal for US issuers")
    if m.warning:
        # Includes notes like "TTM via FY-anchor fallback (...)" or
        # "TTM via YTD+prior-tail fallback (...)" when one of the
        # algebraic fallbacks rescued the window.
        print(f"TTM-calculator note: {m.warning}")

    print(f"{m.value:,.0f} {m.unit} from {len(m.period_facts)} source facts")
```

`has_calculated_q4=True` is normal (most US issuers report YTD in Q3 and an annual 10-K, not a discrete Q4 10-Q). A `ValueError` from `get_pit_value` is the skill's signal that the assembled window doesn't represent a real 12-month aggregate — catch and route around (skip the ticker, try a different `as_of`, or use a different concept).

## Detect amendments in the source facts

Amendments (10-Q/A, 10-K/A) supersede originals once filed, and the PIT filter handles that automatically. To audit when an amendment was actually used:

```python
ts, audit = get_pit_series("AAPL", "Assets", start="2009-01-01", end="2011-12-31",
                            with_audit=True)
amends = audit[audit["source_form_type"].astype(str).str.endswith("/A")]
print(amends[[
    "observation_date", "value", "source_period_end",
    "source_value", "source_filing_date", "source_form_type",
]])
```

Empty frame = no amendment was the most recent fact for any observation in the window. A non-empty frame means the listed observations used the amended value as the latest-known truth (which is correct PIT behavior).

## Iterate over many concepts at one date (one issuer)

Useful for assembling a row of fundamentals for a screen.

```python
asof = "2024-06-30"
concepts = [
    # duration (will TTM-aggregate)
    "Revenues", "GrossProfit", "OperatingIncomeLoss", "NetIncomeLoss",
    "NetCashProvidedByOperatingActivities",
    # instant (will snapshot)
    "Assets", "Liabilities", "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue", "LongTermDebtNoncurrent",
]

row = {}
for c in concepts:
    try:
        m = get_pit_value("AAPL", c, asof)
        row[c] = m.value
    except ValueError as e:
        row[c] = None
        print(f"skip {c}: {e}")
print(row)
```

## Cross-sectional snapshot across many tickers (parallel)

Use `get_pit_value_batch` instead of looping `get_pit_value` — per-ticker EDGAR fetches run concurrently (~2× speedup vs sequential, capped by GIL during XBRL parsing).

```python
asof = "2024-12-31"
tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN"]

ni  = get_pit_value_batch(tickers, "NetIncomeLoss", asof, max_workers=8)        # TTM
eq  = get_pit_value_batch(tickers, "StockholdersEquity", asof, max_workers=8)   # snapshot

screen = (ni[["ticker", "value"]].rename(columns={"value": "ttm_net_income"})
          .merge(eq[["ticker", "value"]].rename(columns={"value": "equity"}),
                 on="ticker", how="outer"))
screen["roe_ttm"] = screen["ttm_net_income"] / screen["equity"]
print(screen.sort_values("roe_ttm", ascending=False))
```

Bad / unsupported tickers are dropped from the result frame (logged at DEBUG); the batch never raises on a single failure.

## Multi-ticker time series (long-format frame)

`get_pit_series_batch` returns a long frame with a `ticker` column prepended — ready for groupby/resample analytics.

```python
ts_long = get_pit_series_batch(
    ["AAPL", "MSFT", "NVDA"], "Revenues",
    start="2020-01-01", end="2024-12-31",
    max_workers=8,
)
# Plot or pivot:
wide = ts_long.pivot(index="period_end", columns="ticker", values="value")
print(wide.tail())

# With audit (which 10-Q / 10-K fed each observation per ticker):
ts_long, audit_long = get_pit_series_batch(
    ["AAPL", "MSFT"], "Revenues",
    start="2023-01-01", with_audit=True,
)
amends = audit_long[audit_long["source_form_type"].astype(str).str.endswith("/A")]
print(f"observations using an amendment: {len(amends)}")
```

## Build a TTM revenue × balance-sheet ratio (TTM Revenues / Equity)

Mixes a duration concept and an instant concept at the same as_of:

```python
asof = "2024-06-30"
rev = get_pit_value("MSFT", "Revenues", asof)            # TTM (duration)
eq  = get_pit_value("MSFT", "StockholdersEquity", asof)   # snapshot (instant)
print(f"TTM revenues: ${rev.value:,.0f}")
print(f"Equity      : ${eq.value:,.0f}")
print(f"Asset turn  : {rev.value / eq.value:.2f}x")
```

The skill applies the same `filing_date <= as_of` filter to both, so the ratio is internally consistent at the chosen point in time.

## Embedding the batch helpers in your own threads

`get_pit_value_batch` and `get_pit_series_batch` are themselves thread-safe: the per-ticker `FinancialFacts` instances are memoised through a thread-safe `lru_cache`, and the EDGAR-session lock only spans `Company(...)` construction so the heavy fact-list materialisation runs in parallel. You can call them from inside your own `ThreadPoolExecutor` without extra synchronisation.

```python
from concurrent.futures import ThreadPoolExecutor

# Three independent screens, each at a different cutoff date — fan out
def screen(asof):
    return get_pit_value_batch(
        ["AAPL","MSFT","NVDA","GOOGL","META"],
        "Assets", asof, max_workers=4,
    )

with ThreadPoolExecutor(max_workers=3) as pool:
    snapshots = list(pool.map(screen, ["2022-12-31","2023-12-31","2024-12-31"]))
# snapshots is a list of 3 DataFrames, one per cutoff
```

If a ticker is requested concurrently across threads, only the first call pays the EDGAR cost; the rest read the cached `FinancialFacts.facts` list under a per-instance `RLock`.

## Save to parquet / CSV

```python
ts, audit = get_pit_series("AAPL", "Assets", with_audit=True)
ts.to_parquet("aapl_assets.parquet", index=False)        # preferred
audit.to_parquet("aapl_assets_audit.parquet", index=False)
```

Or from the CLI:

```bash
python scripts/pit_financials.py AAPL Assets \
    --out aapl_assets.parquet --audit-out aapl_assets_audit.parquet
```

## Composing with the other skills

Build a fully PIT-correct screen across a fund's constituents:

```python
# Skill 1 — fetching-fund-universe
from scripts.fund_holdings import get_fund_holdings, set_identity as _id1
_id1("me@example.com")
holdings = get_fund_holdings("QQQ", dates=["2024-12-31"])
tickers = holdings["ticker_resolved"].dropna().unique()

# Skill 4 — fetching-pit-financials (parallel batch fetches)
from scripts.pit_financials import get_pit_value_batch, set_identity as _id3
_id3("me@example.com")

asof = "2024-12-31"
ni  = get_pit_value_batch(tickers, "NetIncomeLoss", asof, max_workers=8)        # TTM
rev = get_pit_value_batch(tickers, "Revenues", asof, max_workers=8)              # TTM
eq  = get_pit_value_batch(tickers, "StockholdersEquity", asof, max_workers=8)    # snapshot

screen = (ni[["ticker", "value"]].rename(columns={"value": "ttm_ni"})
          .merge(rev[["ticker", "value"]].rename(columns={"value": "ttm_rev"}), on="ticker")
          .merge(eq[["ticker",  "value"]].rename(columns={"value": "equity"}),  on="ticker"))
screen["ni_margin"] = screen["ttm_ni"]  / screen["ttm_rev"]
screen["roe"]       = screen["ttm_ni"]  / screen["equity"]
top = screen.dropna().nlargest(10, "roe")
print(top)
```

Every value in `screen` is what was publicly knowable on `2024-12-31` — no later restatement leaks in.
