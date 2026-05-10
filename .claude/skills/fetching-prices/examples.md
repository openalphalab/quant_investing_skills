# Examples — fetching-prices

Copy-paste recipes for daily-price tasks. No identity / API key required for either source.

## Contents
- Setup
- Single-ticker series
- Single-ticker PIT lookup
- Delisted-ticker fallback (akshare)
- Multi-ticker series (panel)
- Multi-ticker PIT (cross-sectional snapshot)
- Per-ticker source audit
- Total-return calculation
- Align prices to filing-release dates
- CLI variants

## Setup

```python
from scripts.price_history import (
    PriceHistory,
    get_prices, get_prices_batch,
    get_pit_price, get_pit_price_batch,
)
```

## Single-ticker series

```python
# Full available history
px = get_prices("AAPL")

# Windowed
px = get_prices("AAPL", start="2020-01-01", end="2024-12-31")

print(px[["date", "close", "adj_close", "volume", "source"]].tail())
```

## Single-ticker PIT lookup

```python
row = get_pit_price("AAPL", as_of="2024-06-30")
if row is None:
    print("no bar at or before 2024-06-30")
else:
    print(f"close={row['close']:.2f} adj={row['adj_close']:.2f} src={row['source']}")
```

`get_pit_price` returns the latest bar with `date <= as_of`, so it works on weekends and holidays — you'll get the previous trading day automatically.

## Delisted-ticker fallback (akshare)

The same call shape; the fetcher silently routes through akshare when yfinance is empty:

```python
twtr = get_prices("TWTR")
print(twtr["source"].unique())   # ['akshare']
print(twtr["date"].min(), twtr["date"].max())
```

Other recoverable tickers (subject to change as Yahoo's drop list shifts):

```python
for t in ["TWTR", "FRC", "SVB", "SIVB", "ATVI"]:
    df = get_prices(t)
    print(f"{t:5s} rows={len(df):5d} source={df['source'].iloc[0] if len(df) else '—'}")
```

## Multi-ticker series (panel)

```python
panel = get_prices_batch(
    ["AAPL", "MSFT", "NVDA", "GOOGL"],
    start="2020-01-01", end="2024-12-31",
    max_workers=8,
)
print(panel.shape)
print(panel.groupby("ticker")["date"].agg(["min", "max", "count"]))
```

The result is long-format (one row per `ticker × date`); pivot for a wide panel:

```python
import pandas as pd
wide = panel.pivot_table(index="date", columns="ticker", values="adj_close")
returns = wide.pct_change().dropna()
```

## Multi-ticker PIT (cross-sectional snapshot)

```python
snap = get_pit_price_batch(
    ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN"],
    as_of="2024-06-30",
    max_workers=8,
)
print(snap[["ticker", "date", "close", "adj_close", "source"]].to_string(index=False))
```

Tickers with no bar at or before `as_of` are dropped silently — safe to feed in a fund universe where some symbols may not have price history.

## Per-ticker source audit

After a batch fetch, check which tickers landed on each source:

```python
audit = (panel.groupby("ticker")["source"]
         .agg(lambda s: s.value_counts().index[0]))
print("yfinance:", (audit == "yfinance").sum())
print("akshare :", (audit == "akshare").sum())
print(audit[audit == "akshare"])
```

This is the survivorship-bias check: any tickers that ended up on akshare were dropped from Yahoo at some point.

## Total-return calculation

```python
px = get_prices("AAPL", start="2020-01-01", end="2024-12-31")
total_return = px["adj_close"].iloc[-1] / px["adj_close"].iloc[0] - 1
print(f"AAPL total return 2020–2024: {total_return:.1%}")
```

Use `adj_close`, not `close`, for total-return work. `close` is the raw exchange print and crosses through splits without adjustment.

## Align prices to filing-release dates

Compose with `fetching-financial-notices` to get the price on each filing date:

```python
from scripts.financial_notices import get_financial_notices, set_identity
set_identity("me@example.com")

filings = get_financial_notices("AAPL")            # 10-Q / 10-K release calendar
px = get_prices("AAPL")

# As-of-filing-date join: latest price bar at or before each filing
asof_prices = []
for fd in filings["filing_date"]:
    bar = get_pit_price("AAPL", as_of=fd)
    asof_prices.append({"filing_date": fd,
                        "close": bar["close"] if bar is not None else None,
                        "adj_close": bar["adj_close"] if bar is not None else None})
import pandas as pd
joined = filings.merge(pd.DataFrame(asof_prices), on="filing_date")
```

For backtests, this is how you avoid look-ahead: the price observed at the filing release is what a market participant could have acted on.

## CLI variants

```bash
# Save a series window to parquet
python scripts/price_history.py AAPL --start 2018-01-01 --end 2024-12-31 \
    --out aapl.parquet

# Cross-sectional PIT snapshot for a basket
python scripts/price_history.py AAPL MSFT NVDA GOOGL META AMZN \
    --as-of 2024-06-30 --out faang_2024H1.parquet

# Lower workers if rate-limited
python scripts/price_history.py AAPL MSFT NVDA --start 2020-01-01 --workers 2

# Verbose mode prints which source served each ticker
python scripts/price_history.py TWTR -v
```
