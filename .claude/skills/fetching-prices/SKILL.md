---
name: fetching-prices
description: Fetches daily OHLC + volume price history for an equity ticker, with two-source survivorship-bias-free coverage — yfinance is the primary source and akshare is a transparent fallback for tickers delisted before Yahoo's history starts (TWTR after the 2022 take-private, FRC, SVB, etc.). Each row carries a `source` column. Returns a pandas DataFrame for series queries, or the latest bar at-or-before a date for point-in-time lookups. Use when the user asks for stock prices, daily bars, adjusted close, OHLC history, last knowable price as of a date, survivorship-bias-free coverage, delisted-ticker prices, or wants to align fundamentals against a price panel.
---

# Fetching Prices

Pull daily OHLC + volume bars (with split-and-dividend-adjusted close) for any US-listed ticker, including delisted ones. The bundled module is thread-safe, memory-efficient, and safe to import (no network calls until invoked).

The skill exists to be **survivorship-bias-free**: yfinance covers live tickers, akshare's `stock_us_daily` mirror fills in the long tail of issuers whose history Yahoo has dropped (acquired, taken private, defaulted, share-class consolidated). Both are required dependencies — the fallback is unconditional, not guarded by an availability check.

## Contents
- When to use this skill
- Requirements
- Quick start (CLI + Python)
- Two-source fallback (yfinance → akshare)
- Ticker override (share-class formats, merger successors)
- Adjusted vs unadjusted prices
- Workflow
- Output schema
- Multi-ticker batch helpers (parallel fetch)
- Composing with other skills
- Parameters reference
- Troubleshooting
- Files

## When to use this skill

Trigger when the user mentions any of:
- Daily price history, OHLC, adjusted close, volume bars
- "Price of AAPL on 2018-04-12", "TWTR price before delisting"
- Survivorship-bias-free price coverage, delisted ticker prices
- PIT price-as-of joins, aligning prices to a fundamentals panel
- Multi-ticker price panels for a backtest

Do **not** trigger for: intraday bars, options data, dividends as a stream (use XBRL via `fetching-pit-financials`), corporate-action timelines, or non-US listings (akshare's free mirror only covers US tickers).

## Requirements

```bash
pip install -r requirements.txt
```

Both `yfinance` and `akshare` are required — there is no in-process flag to skip one. If you only want yfinance behaviour, the akshare path quietly returns empty for live tickers anyway and adds no measurable cost.

No identity / API key is required for either source.

## Quick start

### CLI

```bash
# Latest bar at or before a date (single PIT lookup)
python scripts/price_history.py AAPL --as-of 2024-06-30

# Series window
python scripts/price_history.py AAPL --start 2018-01-01 --end 2024-12-31

# Delisted-ticker fallback (transparent — same call shape)
python scripts/price_history.py TWTR --as-of 2022-10-27

# Multi-ticker — parallel fetch, long-format frame
python scripts/price_history.py AAPL MSFT NVDA --as-of 2024-06-30 --out snap.parquet
python scripts/price_history.py AAPL MSFT NVDA --start 2020-01-01 --workers 8 --out px.parquet
```

### Python

```python
from scripts.price_history import (
    get_prices, get_prices_batch,
    get_pit_price, get_pit_price_batch,
)

# Series — full history
px = get_prices("AAPL")

# Series — windowed
px = get_prices("AAPL", start="2020-01-01", end="2024-12-31")

# PIT — latest bar at or before a date
row = get_pit_price("AAPL", as_of="2024-06-30")
print(row["close"], row["adj_close"], row["source"])

# Delisted-ticker fallback (same call, akshare under the hood)
px_twtr = get_prices("TWTR")

# Multi-ticker
panel = get_prices_batch(["AAPL", "MSFT", "NVDA"], start="2020-01-01")
snap = get_pit_price_batch(["AAPL", "MSFT", "NVDA"], as_of="2024-06-30")
```

## Two-source fallback (yfinance → akshare)

Resolution order inside `PriceHistory._resolve()`:

1. **yfinance** (`Ticker(symbol).history(period="max", auto_adjust=False, actions=True)`) — the primary source. Bare US tickers, share-class suffixed names (`BRK.B`), and Yahoo-style international suffixes (`ASML.AS`, `2330.TW`) all work.
2. **akshare** (`stock_us_daily(symbol=…, adjust="")` for raw, `adjust="qfq"` for adjusted close) — invoked only when yfinance returns an empty frame. Covers many issuers Yahoo has dropped because they are no longer listed (take-privates, M&A, bankruptcy).

Each row carries a `source` column (`"yfinance"` or `"akshare"`) so downstream code can audit where each bar came from. Examples of tickers that come back from akshare:

| Ticker | Reason |
|---|---|
| `TWTR` | Twitter taken private 2022-10-27 |
| `FRC` | First Republic — FDIC seized 2023-05-01 |
| `SVB` (or `SIVB`) | Silicon Valley Bank — failed 2023-03-10 |
| `ATVI` | Activision — acquired by MSFT 2023-10-13 |

If both sources return empty, the module yields an empty DataFrame with the correct columns rather than raising — safe to feed into a multi-ticker batch where some tickers have no coverage.

Dividend and stock-split streams are intentionally **not** surfaced. akshare's `stock_us_daily` doesn't expose them, and dividends are pulled from XBRL fundamentals via the `fetching-pit-financials` skill anyway. The `qfq`-adjusted close is folded in as `adj_close` so split / dividend total-return analysis still works against a single column.

## Ticker override (share-class formats, merger successors)

A `ticker_overrides.yaml` file **next to `price_history.py`** (i.e. inside the skill's bundled `scripts/` dir, or alongside the script wherever you've vendored it) lets you redirect a tricky symbol through both fetchers before falling back to the original:

```yaml
overrides:
  BFB:  BF-B          # Brown-Forman class B (Yahoo wants the dash)
  BRKB: BRK-B         # Berkshire B (same)
  RTN:  RTX           # Raytheon -> RTX after the UTC merger
  ATVI: MSFT          # silly example — don't actually do this
```

When a ticker has an override, the skill tries the override symbol through yfinance + akshare first, and only falls back to the original if both yield nothing. Adding an override never drops data — it just adds a fetch attempt. When the file is absent the skill falls back to plain symbol resolution with no warning.

## Adjusted vs unadjusted prices

| Column | Meaning |
|---|---|
| `open`, `high`, `low`, `close` | Unadjusted exchange prices — the dollar value the issue actually traded at on `date` |
| `adj_close` | Split-and-dividend-adjusted close, as of the data-source's most recent adjustment epoch |
| `volume` | Daily share volume (not adjusted) |

`adj_close` is what to use for total-return calculations and for any series whose y-axis should be comparable across split events. `close` is what to use for per-share dollar accounting (e.g. matching a known historical headline price, or computing market cap when paired with a same-day shares-outstanding snapshot from `fetching-pit-financials`).

**Important caveat:** `adj_close` reflects splits / dividends through *now*, not through any past `as_of`. If you need a strictly point-in-time adjustment (no future splits leaking back), compute splits from XBRL via `fetching-pit-financials` and apply manually.

## Workflow

```
Price Fetch:
- [ ] Step 1: Confirm ticker(s) and whether tradable today or delisted
- [ ] Step 2: Decide single PIT (--as-of) or series (--start/--end)
- [ ] Step 3: Call get_prices / get_pit_price (or _batch variants)
- [ ] Step 4: Inspect `source` column to confirm which fetcher served each row
- [ ] Step 5: Save or display
```

### Step 1 — Ticker(s)

Bare Yahoo-style symbols. `BRK.B` and `BRK-B` both work via the override; akshare uses uppercase bare symbols (`TWTR`, not `twtr`). For non-US listings (Yahoo `.AS`, `.TW`, `.HK`), only yfinance covers them — akshare US-mirror won't help.

### Step 2 — Series or PIT

- **Series** (`get_prices` / `get_prices_batch`): one row per trading day in `[start, end]`. Pass `start=None, end=None` for the whole history.
- **PIT** (`get_pit_price` / `get_pit_price_batch`): the latest bar with `date <= as_of`. Returns `None` (or drops the ticker from a batch) if no bar exists by that date.

`--as-of` is mutually exclusive with `--start` / `--end` on the CLI; in Python you choose by which function you call.

### Step 3 — Fetch

```python
px = get_prices("AAPL", start="2020-01-01")
row = get_pit_price("AAPL", as_of="2024-06-30")
```

`PriceHistory(ticker)` is the underlying class — the functional façade caches one instance per ticker via `lru_cache`, so repeat calls inside one session reuse the network round-trip.

### Step 4 — Inspect

Always check the `source` column when you suspect a ticker may have been delisted. A row that came from akshare may differ slightly in adjusted-close from a yfinance row for the same date (different mirror, different adjustment epoch).

### Step 5 — Output

Default: print `df.head()` and `df.shape`. For persistence, prefer parquet for speed and dtype fidelity.

## Output schema

### `get_prices(ticker, *, start=None, end=None)` → `pd.DataFrame`

| Column | Type | Notes |
|---|---|---|
| `date` | `datetime.date` | Trading date — timezone-stripped if yfinance returned a tz-aware index |
| `open`, `high`, `low`, `close` | `float` | Unadjusted exchange prices in the issue's currency |
| `adj_close` | `float` | Split-and-dividend-adjusted close; may be `NaN` if akshare's qfq mirror has no data |
| `volume` | `float` | Daily share volume |
| `source` | `str` | `"yfinance"` or `"akshare"` |

Sorted by `date` ascending. Empty frame (correct columns) when neither source has data.

### `get_pit_price(ticker, as_of)` → `pd.Series` or `None`

A single row (Series indexed by the canonical column set) or `None` when no bar exists at or before `as_of`.

### Batch returns

`get_prices_batch(...)` and `get_pit_price_batch(...)` return long-format frames with a leading `ticker` column. Per-ticker failures are logged at DEBUG and the offending ticker is dropped — the batch never raises on a single bad ticker.

## Multi-ticker batch helpers (parallel fetch)

```python
panel = get_prices_batch(
    ["AAPL", "MSFT", "NVDA", "GOOGL"],
    start="2020-01-01", end="2024-12-31",
    max_workers=8,
)

snap = get_pit_price_batch(
    ["AAPL", "MSFT", "NVDA", "GOOGL"],
    as_of="2024-06-30",
    max_workers=8,
)
```

The batch dedups + strips + sorts the input, runs per-ticker fetches concurrently via `ThreadPoolExecutor`, and concatenates into a long frame. yfinance and akshare share separate module-level locks (`_YF_LOCK`, `_AK_LOCK`) only across their entry points; the heavy network work runs lock-free, so concurrent tickers actually parallelise.

Drop `max_workers` to 1–2 if either source rate-limits you.

## Composing with other skills

Price + fundamentals panel:

```python
from scripts.fund_holdings import get_fund_holdings, set_identity as _id1
from scripts.price_history import get_pit_price_batch
from scripts.pit_financials import get_pit_value_batch, set_identity as _id2

_id1("me@example.com")
_id2("me@example.com")

holdings = get_fund_holdings("QQQ", dates=["2024-12-31"])
tickers = holdings["ticker_resolved"].dropna().unique().tolist()

asof = "2024-12-31"
px = get_pit_price_batch(tickers, asof, max_workers=8)
ni = get_pit_value_batch(tickers, "NetIncomeLoss", asof, max_workers=8)
eq = get_pit_value_batch(tickers, "StockholdersEquity", asof, max_workers=8)

panel = (px[["ticker", "close", "adj_close"]]
         .merge(ni[["ticker", "value"]].rename(columns={"value": "ttm_ni"}),
                on="ticker", how="left")
         .merge(eq[["ticker", "value"]].rename(columns={"value": "equity"}),
                on="ticker", how="left"))
```

Pair with `fetching-financial-notices` to align bars to filing-release dates rather than calendar dates.

## Parameters reference

`get_prices(ticker, *, start=None, end=None)`

| Parameter | Type | Notes |
|---|---|---|
| `ticker` | `str` | Non-empty. Whitespace stripped. Validated via decorator. |
| `start`, `end` | date-like or `None` | Inclusive trading-date window. `None` = no bound. Accepts str / date / datetime / Timestamp. |

`get_pit_price(ticker, as_of)` — same `ticker` semantics; `as_of` is required.

`get_prices_batch(tickers, *, start=None, end=None, max_workers=8)` — parallel multi-ticker series.

`get_pit_price_batch(tickers, as_of, *, max_workers=8)` — parallel multi-ticker PIT.

| Parameter | Type | Notes |
|---|---|---|
| `tickers` | iterable of `str` | Deduplicated and whitespace-stripped. Empty / non-string entries dropped. |
| `max_workers` | `int` | Parallel ticker fetches (default 8). Drop to 1–2 on rate limits. |

`PriceHistory(ticker)` — the underlying class; `.history` is the cached full frame, `.load(start=, end=)` returns the windowed slice, `.pit(as_of)` returns the single PIT row. Constructed transparently per ticker via an LRU cache in the functional façade.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Empty DataFrame for a clearly-tradable ticker | Check Yahoo for the symbol; confirm capitalisation; try the override (e.g. `BRK-B` instead of `BRKB`) |
| Empty DataFrame for a delisted ticker | akshare's mirror sometimes lags or drops obscure issuers. Try the override, or check whether the ticker was reused for a different issuer |
| `adj_close` is `NaN` for akshare-served rows | The qfq mirror had no data — accept `NaN` or fall back to using `close` for total-return |
| HTTP rate-limit from yfinance | Drop `max_workers` to 1–2; re-run after a few minutes |
| `ModuleNotFoundError: akshare` | Required dep — run `pip install -r requirements.txt` |
| Source column shows `akshare` for a live ticker | yfinance temporarily returned empty (network issue or rate limit). Re-run; the LRU cache holds the result for the session |
| Different `adj_close` between two runs | yfinance updates its adjustment epoch on each split / dividend; this is expected. For reproducible historical adjustments, snapshot the data and persist it |
| Foreign listing returns empty | akshare doesn't cover non-US tickers; only yfinance does. Confirm the Yahoo suffix (`.AS`, `.TW`, …) is correct |

## Files

- `scripts/price_history.py` — main module + CLI (execute or import)
- `examples.md` — copy-paste recipes for series, PIT, batch, and survivorship-bias scenarios
- `requirements.txt` — pinned dependencies
