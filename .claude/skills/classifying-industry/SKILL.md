---
name: classifying-industry
version: 0.1.0
license: MIT
author: ganchi.zhang@gmail.com
description: Enriches NPORT-P fund holdings (or any DataFrame keyed by ticker / ISIN) with industry classifications from two authoritative taxonomies — financedatabase (sector, industry_group, industry; MSCI-style) and SEC EDGAR (sic, sic_industry; regulatory). Accepts either a holdings DataFrame (e.g. from the fetching-fund-universe skill) or a fund ticker string (fetched internally via fund_holdings). Returns a pandas DataFrame keyed by (ticker, isin). Use when the user asks to classify stocks by sector or industry, map tickers to SIC codes, attach MSCI-style taxonomy to a universe, look up sector/industry for fund holdings, or group equities by industry.
---

# Classifying Industry

Attach sector / industry / SIC classifications to a fund universe. Composes naturally with `fetching-fund-universe`: pass that skill's DataFrame straight in, or pass a fund ticker and this skill will fetch the holdings itself.

## Contents
- When to use this skill
- Requirements
- Quick start (CLI + Python)
- Workflow
- Output schema
- Composing with fetching-fund-universe
- Parameters reference
- Troubleshooting
- Files

## When to use this skill

Trigger when the user mentions any of:
- Sector / industry / GICS / SIC classification of stocks
- "What industry is AAPL in?" / "Group these tickers by sector"
- Attaching industry metadata to a holdings or universe table
- SEC SIC codes, SIC industry labels
- MSCI-style `sector` / `industry_group` / `industry` fields

Do **not** trigger for: fundamental financials, price history, options, insider transactions. Those belong to other skills.

## Requirements

```bash
pip install -r requirements.txt
```

SEC lookups require a contact email (mandated by EDGAR):
- `--identity me@example.com` CLI flag
- `EDGAR_IDENTITY=me@example.com` env var
- `set_identity("me@example.com")` in Python, before the first call

## Quick start

### CLI

```bash
# Fetch a fund's holdings and classify in one shot
python scripts/industry_classifications.py SPY --identity me@example.com

# Same, persisted
python scripts/industry_classifications.py SPY --out spy_industry.parquet \
    --identity me@example.com

# Enrich a parquet/csv from disk (fetching-fund-universe output)
python scripts/industry_classifications.py --from spy_holdings.parquet \
    --out spy_industry.parquet --identity me@example.com
```

### Python

```python
from scripts.industry_classifications import get_industry_table, set_identity

set_identity("me@example.com")

# 1. Fund ticker — fetched internally via fund_holdings
df = get_industry_table("SPY")

# 2. Existing holdings DataFrame (from fetching-fund-universe)
df = get_industry_table(holdings_df)
```

## Workflow

```
Industry Classification:
- [ ] Step 1: Decide between fund ticker or existing DataFrame
- [ ] Step 2: Ensure an EDGAR identity is configured
- [ ] Step 3: Call get_industry_table(source)
- [ ] Step 4: Save or display the result
```

### Step 1 — Source

Pass whichever is most convenient:
- A fund ticker `str` — the skill calls `get_fund_holdings(ticker)` internally and uses every NPORT-P filing.
- A holdings DataFrame — the enricher picks up `ticker_resolved` (preferred) or `ticker`, plus `isin` and `name`. Other columns are ignored.

The DataFrame path also activates a **name-based fd validation** step: if the input frame carries a `name` column (NPORT does), and financedatabase's name for the matched ISIN/ticker shares no meaningful tokens with the NPORT name, the fd columns are nulled out for that row. This catches fd's occasional ticker collisions (e.g. fd at one point pointed `ZWS` to "Telia Lietuva" instead of "Zurn Elkay").

### Step 2 — Identity

`set_identity` must run once per process before any SEC lookup. If `EDGAR_IDENTITY` is set, the CLI uses it automatically.

### Step 3 — Enrich

Always use `get_industry_table`; do not re-implement the join:

```python
df = get_industry_table(source)
```

Under the hood:
1. **financedatabase** is loaded once per process (~160k equities, LRU-cached) and joined on **(ticker, isin)** first, then ISIN alone, then ticker alone — a three-stage disambiguating join that pins the right venue for ISINs that fd duplicates across listings.
2. fd's placeholder stub rows (name in `{"one", "two"}`) are dropped before the join — they otherwise poison ticker-only matches.
3. **Name-based validation** nulls fd columns when the input `name` and fd `name` share no meaningful tokens (only fires when input has a `name` column).
4. **SEC `Company(ticker)`** is called in parallel (8 workers by default, LRU-cached) for `sic` + `sic_industry`.

### Step 4 — Output

Default: `print(df.head())` and `df.shape`. For persistence use parquet (`df.to_parquet("out.parquet", index=False)`).

## Output schema

One row per unique `(ticker, isin)` input pair:

| Column | Source | Example |
|---|---|---|
| `ticker` | input | `NVDA` |
| `isin` | input | `US67066G1040` |
| `name` | financedatabase | `NVIDIA Corporation` |
| `sector` | financedatabase | `Information Technology` |
| `industry_group` | financedatabase | `Semiconductors & Semiconductor Equipment` |
| `industry` | financedatabase | `Semiconductors` |
| `sic` | SEC EDGAR | `3674` |
| `sic_industry` | SEC EDGAR | `Semiconductors & Related Devices` |

Missing values are `NA` (never mocked). A row may have fd columns populated but SEC columns blank (foreign equity not registered with the SEC) or vice versa. The dedupe key is `(ticker, isin)`, so the same issuer with the same ticker but a different ISIN (rare, typically share-class re-coding) yields two rows.

## Composing with fetching-fund-universe

Two natural patterns:

```python
# Pattern A: let this skill drive the fetch
from scripts.industry_classifications import get_industry_table, set_identity
set_identity("me@example.com")
df = get_industry_table("QQQ")    # fetches and enriches in one call
```

```python
# Pattern B: reuse an existing holdings frame
from scripts.fund_holdings import get_fund_holdings, set_identity as _fh_id
_fh_id("me@example.com")
holdings = get_fund_holdings("QQQ", dates=["2024-12-31"])

from scripts.industry_classifications import get_industry_table, set_identity
set_identity("me@example.com")
industry = get_industry_table(holdings)

# Merge for per-position analytics
merged = holdings.merge(industry, on=["ticker", "isin"], how="left")
sector_weights = merged.groupby("sector")["pct_value"].sum().sort_values(ascending=False)
```

Pattern B is preferred when you've already fetched holdings (e.g. you want only specific reporting dates), since the str-path always pulls every filing.

## Parameters reference

`get_industry_table(holdings, *, max_workers=8)`

| Parameter | Type | Notes |
|---|---|---|
| `holdings` | `pd.DataFrame` or `str` | DataFrame: inspected for `ticker_resolved`/`ticker`, `isin`, and (optionally) `name` columns; other columns ignored. String: a fund ticker (e.g. `"SPY"`) — fetched internally via `get_fund_holdings`. |
| `max_workers` | `int` | Parallel SEC lookups (default 8). Drop to 1–2 if you hit HTTP 429. |

`load_equities_reference()` — returns the raw financedatabase frame (~160k rows), LRU-cached. Useful for ad-hoc filtering beyond what this skill exposes.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `IdentityError` from edgar | Call `set_identity("email")` or set `EDGAR_IDENTITY` first |
| `ModuleNotFoundError: financedatabase` | Required dep, not optional. `pip install -r requirements.txt` |
| `ModuleNotFoundError: fund_holdings` | The skill's `scripts/` dir is missing `fund_holdings.py`; re-sync from `fetching-fund-universe` or run from a directory where it's importable. |
| Many rows with empty fd columns even though the names match | Check whether your input has a `name` column whose strings differ stylistically from fd's — the name-validation step compares meaningful tokens, but a totally non-overlapping styling can cause false negatives. |
| Empty fd columns on every row | Check the `isin` column in the input — if all blank, fd falls back to ticker but foreign listings will miss |
| Empty SEC columns on every row | Tickers may not be SEC-registered (ADRs, foreign equities). Not an error. |
| HTTP 429 from SEC | Rerun with `max_workers=1` or `2` |
| Slow first call | `load_equities_reference()` builds the fd frame once per process. Subsequent calls are free. |

## Files

- `scripts/industry_classifications.py` — main module + CLI (execute or import)
- `scripts/fund_holdings.py` — bundled NPORT-P fetcher (imported when `holdings` is a str)
- `examples.md` — copy-paste recipes, including chaining with `fetching-fund-universe`
- `requirements.txt` — pinned dependencies
