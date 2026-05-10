# Examples — classifying-industry

Copy-paste recipes. Every snippet assumes an identity is configured (via `set_identity(...)` or `$EDGAR_IDENTITY`).

## Contents
- Setup
- Classify a fund by ticker (one-liner)
- Enrich an existing holdings DataFrame
- Merge classifications back onto holdings
- Sector weights for a fund
- Filter equities by SIC range
- Use the raw financedatabase frame
- Persist results
- CLI variants

## Setup

```python
from scripts.industry_classifications import (
    get_industry_table,
    load_equities_reference,
    set_identity,
)

set_identity("me@example.com")
```

## Classify a fund by ticker (one-liner)

The fastest path — passes the ticker straight through to `get_fund_holdings` internally and enriches the result. Uses every NPORT-P filing for that fund.

```python
df = get_industry_table("SPY")
print(df[["ticker", "sector", "industry", "sic", "sic_industry"]].head())
```

If you want to control the date window, fetch holdings yourself and pass the DataFrame in (next recipe).

## Enrich an existing holdings DataFrame

When you already have holdings (e.g. you only want a specific reporting date), pass the DataFrame in directly. The enricher reads `ticker_resolved`/`ticker`, `isin`, and `name` (used for fd validation).

```python
from scripts.fund_holdings import get_fund_holdings, set_identity as _fh_id
_fh_id("me@example.com")
holdings = get_fund_holdings("QQQ", dates=["2024-12-31"])

industry = get_industry_table(holdings)
print(industry.head())
```

The presence of `name` activates the name-based fd validation: rows where fd's name shares no meaningful tokens with the NPORT name get their fd columns nulled out, catching fd's occasional ticker collisions.

## Merge classifications back onto holdings

```python
merged = holdings.merge(industry, on=["ticker", "isin"], how="left")
```

After the merge, `merged` has every NPORT field plus `sector`, `industry_group`, `industry`, `sic`, `sic_industry`.

## Sector weights for a fund

```python
weights = (merged
           .groupby("sector")["pct_value"]
           .sum()
           .sort_values(ascending=False)
           .round(2))
print(weights)
```

## Filter equities by SIC range

```python
# Manufacturing is SIC 2000-3999
mfg = industry[industry["sic"].str.startswith(("2", "3"), na=False)]
```

## Use the raw financedatabase frame

For ad-hoc filtering beyond what `get_industry_table` exposes:

```python
eq = load_equities_reference()
us_semis = eq[(eq["country"] == "United States") &
              (eq["industry"] == "Semiconductors")]
```

## Persist results

```python
industry.to_parquet("qqq_industry.parquet", index=False)   # preferred
industry.to_csv("qqq_industry.csv", index=False)
```

## CLI variants

```bash
# Fund ticker — fetched and enriched in one shot
python scripts/industry_classifications.py SPY \
    --out spy_industry.parquet --identity me@example.com

# Existing holdings file (parquet or csv)
python scripts/industry_classifications.py --from qqq_holdings.parquet \
    --out qqq_industry.parquet --identity me@example.com

# Lower SEC concurrency (rate-limit recovery)
python scripts/industry_classifications.py SPY --workers 2 \
    --out spy_industry.parquet --identity me@example.com
```
