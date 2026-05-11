---
name: fetching-fund-universe
version: 0.1.0
license: MIT
author: openalphalab@gmail.com
description: Fetches fund and ETF holdings universes from SEC EDGAR NPORT-P filings for a given fund ticker (e.g., SPY, QQQ, VOO, IWM, VTI), with optional filtering by filing date. Returns a pandas DataFrame containing every portfolio holding plus filling_date, reporting_date, and ticker_resolved (NPORT ticker, falling back to a yfinance ISIN lookup). Use when the user asks to build a stock universe from a fund, inspect ETF composition, retrieve fund holdings, pull NPORT-P filings, analyse portfolio snapshots as of a specific date, or compare fund holdings across reporting periods.
---

# Fetching Fund Universe

Pull mutual-fund / ETF holdings from SEC EDGAR NPORT-P filings and return them as a single pandas DataFrame. The bundled Python module is thread-safe, memory-efficient, and safe to import (no network calls until invoked).

## Contents
- When to use this skill
- Requirements
- Quick start (CLI + Python)
- Workflow
- Capabilities (list of recipes)
- Parameters reference
- Troubleshooting
- Files

## When to use this skill

Trigger when the user mentions any of:
- Fund holdings, ETF holdings, mutual-fund composition
- NPORT-P filings, N-PORT, SEC investment filings
- Phrases like "what does SPY hold", "holdings of QQQ on 2024-03-31", "build a universe from a fund"
- Historical portfolio snapshots for a fund ticker
- Comparing holdings across reporting dates

Do **not** trigger for: single-stock fundamentals, 10-K/10-Q filings, XBRL financials, insider transactions. Those belong to other skills.

## Requirements

Install dependencies once:

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
# All historical filings, printed to stdout
python scripts/fund_holdings.py SPY --identity me@example.com

# Specific filing dates, saved to parquet
python scripts/fund_holdings.py SPY --dates 2024-03-31 2024-06-30 --out spy.parquet

# Via environment variable
EDGAR_IDENTITY=me@example.com python scripts/fund_holdings.py QQQ --out qqq.csv
```

### Python

```python
from scripts.fund_holdings import get_fund_holdings, set_identity

set_identity("me@example.com")
df = get_fund_holdings("SPY", dates=["2024-03-31"])
print(df.shape)
```

Returned DataFrame contains every investment row from each matching filing, plus:
- `filling_date` — SEC filing date
- `reporting_date` — portfolio as-of date
- `ticker_resolved` — preferred ticker column: the NPORT `ticker` when present and well-formed, else a yfinance-by-ISIN lookup. Pass `resolve_tickers=False` to skip the Yahoo calls. The fallback also kicks in for two flavours of unusable NPORT `ticker`:
  - **Right-padded** (e.g. `LRCXXXXX`, `SLGXXXX`, `BLKXXXX`) — some filers (incl. State Street's SPY) right-pad with `X`'s; routed through the ISIN lookup so EDGAR XBRL works downstream.
  - **CUSIP-synthetic / paired-off** (e.g. `F104PAIROFF` for Discovery Inc, `L100PS` for Arconic) — anything that doesn't match `[A-Z]+([.-][A-Z]+)?` is treated as synthetic.

  The yfinance lookup prefers the **unsuffixed** (US) symbol from Yahoo's quote list rather than the first match, so US issuers don't accidentally land on a foreign secondary listing (e.g. `LRCX`, not `LRCX.MX`).

## Workflow

Copy this checklist and tick items as you progress:

```
Fund Universe Fetch:
- [ ] Step 1: Confirm the ticker(s) the user wants
- [ ] Step 2: Confirm the date selection (specific list, or "all")
- [ ] Step 3: Ensure an EDGAR identity is configured
- [ ] Step 4: Call get_fund_holdings(ticker, dates=...)
- [ ] Step 5: Save or display the resulting DataFrame
```

### Step 1 — Confirm the ticker

If the user says "the S&P 500 ETF", map to `SPY`. If ambiguous ("tech fund"), ask for the exact ticker. Tickers are strings like `"SPY"`, `"QQQ"`, `"VOO"`.

### Step 2 — Confirm the dates

If the user says "latest", pass `dates=None` (returns every filing). If they give specific dates, pass a list of `YYYY-MM-DD` strings — they are matched against the SEC **filing_date**, not the reporting period. `date`, `datetime`, and `pandas.Timestamp` are also accepted.

### Step 3 — Identity

`set_identity` must be called once per process. If `EDGAR_IDENTITY` is set in the environment, use it; otherwise ask the user for an email.

### Step 4 — Fetch

Always use the bundled helper rather than re-implementing the loop:

```python
df = get_fund_holdings("SPY", dates=["2024-03-31", "2024-06-30"])
```

Filings are downloaded in parallel via a `ThreadPoolExecutor` and the output preserves filing-index order. Lower `max_workers` to `1` or `2` if you hit HTTP 429.

### Step 5 — Output

Default to showing `df.head()` and `df.shape`. For persistence, prefer parquet (`df.to_parquet("out.parquet", index=False)`) over CSV for speed and type fidelity.

## Capabilities

Concrete recipes this skill supports. See [examples.md](examples.md) for full copy-paste code.

| Capability | Entry point |
|---|---|
| Fetch every historical filing for a fund | `get_fund_holdings("SPY")` |
| Fetch holdings on specific filing dates | `get_fund_holdings("SPY", dates=[...])` |
| Iterate across multiple funds | loop over `get_fund_holdings` |
| Reuse cached filings across calls | `FundHoldings(ticker).load(...)` |
| Throttle to sequential mode | `max_workers=1` |
| Persist to parquet / csv | `--out` CLI flag or `df.to_parquet` |
| Filter by asset class / country | standard pandas on returned DataFrame |
| Skip the yfinance ISIN->ticker fallback | `resolve_tickers=False` (or `--no-resolve-tickers`) |

## Parameters reference

`get_fund_holdings(ticker, dates=None, *, max_workers=8, resolve_tickers=True)`

| Parameter | Type | Notes |
|---|---|---|
| `ticker` | `str` | Non-empty. Whitespace stripped. Validated via decorator. |
| `dates` | iterable or `None` | Accepts str / date / datetime / Timestamp. `None` = all filings. Matched against `filing_date`. |
| `max_workers` | `int` | Parallel download cap. Auto-clamped to `len(dates)`. Also caps concurrent yfinance ISIN lookups. |
| `resolve_tickers` | `bool` | Default `True`. Adds `ticker_resolved` column filled via yfinance for rows whose NPORT `ticker` is NA/blank. Disable with `False` (or `--no-resolve-tickers`) to skip Yahoo calls. Lookups are LRU-cached across calls in the same process. |

`FundHoldings(ticker).load(dates=None, *, max_workers=8, resolve_tickers=True)` — same semantics; the instance caches its filing list so repeat `.load()` calls skip the initial lookup.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `IdentityError` / missing identity | Call `set_identity("email")` or set `EDGAR_IDENTITY` before any call |
| Empty DataFrame | No filings matched the requested dates — widen or drop the filter |
| HTTP 429 rate-limit | Rerun with `max_workers=1` or `2` |
| `ValueError: ticker must be a non-empty str` | Pass the ticker as a non-empty string literal |
| `ModuleNotFoundError: edgar` | `pip install -r requirements.txt` (package name is `edgartools`) |
| `ticker_resolved` column missing or all-null | `yfinance` not installed, or `resolve_tickers=False` — run `pip install yfinance` to re-enable |
| Slow load on international funds | First call per ISIN hits Yahoo; cached thereafter. Raise `max_workers` or set `resolve_tickers=False` for speed. |

## Files

- `scripts/fund_holdings.py` — main module + CLI (execute or import)
- `examples.md` — copy-paste recipes for common tasks
- `requirements.txt` — pinned dependencies
