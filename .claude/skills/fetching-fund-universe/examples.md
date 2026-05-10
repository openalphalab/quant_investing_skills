# Examples — fetching-fund-universe

Copy-paste recipes for common fund-universe tasks. Every snippet assumes an identity has already been configured (via `set_identity(...)` or `$EDGAR_IDENTITY`).

## Contents
- Setup
- Fetch every historical filing
- Fetch specific filing dates
- Multi-ticker universe
- Reuse an instance for several date queries
- Save to parquet / CSV
- Control parallelism
- Filter holdings by asset class
- Compute holding deltas between two dates
- Use the resolved ticker column
- Skip the yfinance fallback

## Setup

```python
from scripts.fund_holdings import FundHoldings, get_fund_holdings, set_identity

set_identity("me@example.com")
```

## Fetch every historical filing

```python
df = get_fund_holdings("SPY")
print(df.shape)
print(df["reporting_date"].unique())
```

## Fetch specific filing dates

```python
df = get_fund_holdings("SPY", dates=["2024-03-31", "2024-06-30"])
```

Accepts mixed date types:

```python
import pandas as pd
from datetime import date
df = get_fund_holdings("SPY", dates=[date(2024, 3, 31), pd.Timestamp("2024-06-30")])
```

## Multi-ticker universe

```python
import pandas as pd

tickers = ["SPY", "QQQ", "IWM"]
frames = []
for t in tickers:
    d = get_fund_holdings(t, dates=["2024-03-31"])
    d["fund"] = t
    frames.append(d)
universe = pd.concat(frames, ignore_index=True)
```

## Reuse an instance for several date queries

Filings are fetched once and cached on the instance:

```python
fh = FundHoldings("SPY")
q1 = fh.load(dates=["2024-03-31"])
q2 = fh.load(dates=["2024-06-30"])
q3 = fh.load(dates=["2024-09-30"])
```

## Save to parquet / CSV

```python
df = get_fund_holdings("SPY", dates=["2024-03-31"])
df.to_parquet("spy_2024Q1.parquet", index=False)   # preferred
df.to_csv("spy_2024Q1.csv", index=False)
```

Or from the CLI:

```bash
python scripts/fund_holdings.py SPY --dates 2024-03-31 --out spy_2024Q1.parquet
```

## Control parallelism

```python
df = get_fund_holdings("SPY", max_workers=1)    # strict sequential
df = get_fund_holdings("SPY", max_workers=16)   # aggressive parallel
```

Drop to `max_workers=1` if EDGAR returns HTTP 429.

## Filter holdings by asset class

The returned DataFrame exposes every NPORT-P field; filter with standard pandas:

```python
df = get_fund_holdings("SPY", dates=["2024-03-31"])
equities = df[df["asset_category"] == "EC"]      # equity common
top50 = equities.nlargest(50, "pct_value")        # largest by % of fund value
```

## Use the resolved ticker column

`ticker_resolved` is populated by default: existing NPORT `ticker` when present,
else a yfinance ISIN lookup. Foreign equities come back with Yahoo exchange
suffixes (`.TW`, `.AS`, `.HK`, …), so it's the right column to feed into any
downstream Yahoo-based pipeline.

```python
df = get_fund_holdings("VXUS", dates=["2024-12-31"])

# How many rows got filled by the yfinance fallback?
filled = df["ticker"].isna() & df["ticker_resolved"].notna()
print(f"Yahoo fallback filled {filled.sum()} / {len(df)} rows")

# Use it as the canonical identifier
universe = df.dropna(subset=["ticker_resolved"])["ticker_resolved"].unique()
```

## Skip the yfinance fallback

Disable the Yahoo lookup when you only care about the raw NPORT fields or want
to avoid external traffic:

```python
df = get_fund_holdings("VXUS", dates=["2024-12-31"], resolve_tickers=False)
# 'ticker_resolved' column is absent in this mode
```

From the CLI:

```bash
python scripts/fund_holdings.py VXUS --dates 2024-12-31 --no-resolve-tickers --out vxus.parquet
```

## Compute holding deltas between two dates

```python
df = get_fund_holdings("SPY", dates=["2024-03-31", "2024-06-30"])
pivot = df.pivot_table(
    index="ticker",
    columns="reporting_date",
    values="pct_value",
    aggfunc="sum",
    fill_value=0.0,
)
pivot["delta"] = pivot.iloc[:, -1] - pivot.iloc[:, 0]
entries = pivot[pivot.iloc[:, 0].eq(0) & pivot.iloc[:, -1].gt(0)]
exits   = pivot[pivot.iloc[:, 0].gt(0) & pivot.iloc[:, -1].eq(0)]
```
