"""Point-in-time daily price history (OHLC + volume) from yfinance + akshare.

For each ticker, pull the full daily history of unadjusted ``open / high /
low / close``, split-and-dividend-adjusted ``adj_close``, and ``volume``.
Use ``--as-of`` for a single point-in-time snapshot (the latest bar at or
before the date), or ``--start`` / ``--end`` for a window of bars.

Two-source fallback for survivorship-bias-free coverage:

* ``yfinance`` is the primary source — fast, broad, free.
* ``akshare`` (``stock_us_daily``) is the fallback when yfinance returns an
  empty frame, which is what happens for tickers that were delisted before
  Yahoo's history begins (TWTR after the 2022 take-private, FRC, SVB, etc.).

Each row carries a ``source`` column (``"yfinance"`` or ``"akshare"``) so
downstream code can audit where its data came from. Both libraries are
required dependencies — ``akshare`` is what keeps the universe
survivorship-bias-free, so the fallback is unconditional rather than
guarded by an availability check.

Dividend and stock-split streams are intentionally not surfaced here —
``akshare.stock_us_daily`` does not expose them, and dividends are pulled
from XBRL fundamentals via the ``fetching-pit-financials`` skill anyway.

CLI:
    python price_history.py AAPL
    python price_history.py AAPL --start 2018-01-01 --end 2024-12-31
    python price_history.py AAPL --as-of 2024-06-30
    python price_history.py TWTR --as-of 2022-10-27        # akshare fallback
    python price_history.py AAPL MSFT NVDA --as-of 2024-06-30 --out snap.parquet
    python price_history.py AAPL MSFT NVDA --start 2020-01-01 --workers 8 --out px.parquet

Library:
    from price_history import (
        get_prices, get_prices_batch,
        get_pit_price, get_pit_price_batch,
    )
    px = get_prices("AAPL", start="2020-01-01")
    snap = get_pit_price("AAPL", as_of="2024-06-30")
    delisted = get_prices("TWTR")                              # via akshare
    panel = get_prices_batch(["AAPL", "MSFT", "NVDA"], start="2020-01-01")
    snap_batch = get_pit_price_batch(["AAPL", "MSFT", "NVDA"], as_of="2024-06-30")
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from functools import lru_cache, wraps
from threading import RLock
from typing import Any, Callable, Iterable

import akshare as _ak
import pandas as pd
import yfinance as yf

__all__ = [
    "PriceHistory",
    "get_prices",
    "get_prices_batch",
    "get_pit_price",
    "get_pit_price_batch",
]

_LOG = logging.getLogger(__name__)
_YF_LOCK = RLock()  # serialises yfinance.Ticker construction (shared session/cache)
_AK_LOCK = RLock()  # serialises akshare.stock_us_daily (module-level request state)

_OUT_COLS: tuple[str, ...] = (
    "date", "open", "high", "low", "close", "adj_close", "volume", "source",
)
_BATCH_COLS: tuple[str, ...] = ("ticker", *_OUT_COLS)


# ── Decorators ─────────────────────────────────────────────────────────────
def _timed(fn: Callable) -> Callable:
    """DEBUG-level wall-clock timing; zero cost when logging is disabled."""
    @wraps(fn)
    def _w(*a, **kw):
        t = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            _LOG.debug("%s %.3fs", fn.__qualname__, time.perf_counter() - t)
    return _w


def _validate_ticker(fn: Callable) -> Callable:
    """Reject empty / non-string tickers before any network I/O."""
    @wraps(fn)
    def _w(ticker, *a, **kw):
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError(f"ticker must be a non-empty str, got {ticker!r}")
        return fn(ticker.strip(), *a, **kw)
    return _w


# ── Helpers ────────────────────────────────────────────────────────────────
def _as_date(x) -> date:
    """Coerce str | datetime | date | pandas.Timestamp -> datetime.date."""
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return pd.Timestamp(x).date()


def _empty_frame() -> pd.DataFrame:
    """Empty canonical frame — used by both fetchers' early-out paths."""
    return pd.DataFrame(columns=list(_OUT_COLS))


# ── Ticker override (share-class / merger successor) ──────────────────────
# Loaded once at first use from `ticker_overrides.yaml` next to this script.
# Empty dict when the file is absent; intentional — the skill stays standalone.
@lru_cache(maxsize=1)
def _ticker_overrides() -> dict[str, str]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ticker_overrides.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml  # local import: yaml isn't a hard dep of step_5 otherwise
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        _LOG.warning("could not load %s: %s", path, e)
        return {}
    return {str(k).upper(): str(v)
            for k, v in (spec.get("overrides") or {}).items()}


def _ticker_override_for(ticker: str) -> str | None:
    return _ticker_overrides().get(ticker.strip().upper())


# ── yfinance source ────────────────────────────────────────────────────────
def _normalise_yf(raw: pd.DataFrame) -> pd.DataFrame:
    """Reshape yfinance's ``.history()`` frame into the canonical schema."""
    if raw is None or raw.empty:
        return _empty_frame()
    idx = raw.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out = pd.DataFrame({
        "date": [_as_date(d) for d in idx],
        "open": raw.get("Open"),
        "high": raw.get("High"),
        "low": raw.get("Low"),
        "close": raw.get("Close"),
        "adj_close": raw.get("Adj Close"),
        "volume": raw.get("Volume"),
        "source": "yfinance",
    })
    return (out[list(_OUT_COLS)]
            .sort_values("date", kind="mergesort")
            .reset_index(drop=True))


def _fetch_yf(ticker: str) -> pd.DataFrame:
    """One yfinance round-trip for the full daily history; ``period='max'``."""
    with _YF_LOCK:
        tkr = yf.Ticker(ticker)
    raw = tkr.history(period="max", auto_adjust=False, actions=True)
    return _normalise_yf(raw)


# ── akshare source (delisted-ticker fallback) ─────────────────────────────
def _normalise_akshare(
    unadj: pd.DataFrame | None, adj: pd.DataFrame | None,
) -> pd.DataFrame:
    """Merge akshare's two daily frames into the canonical schema.

    ``ak.stock_us_daily`` returns ``date / open / high / low / close /
    volume``. Calling it with ``adjust=""`` gives raw exchange prices;
    ``adjust="qfq"`` gives the same columns split-and-dividend-adjusted.
    We take OHLC + volume from the raw call (true to "unadjusted") and
    splice the qfq ``close`` in as ``adj_close``.
    """
    if unadj is None or unadj.empty:
        return _empty_frame()
    base = pd.DataFrame({
        "date": [_as_date(d) for d in unadj["date"]],
        "open": pd.to_numeric(unadj["open"], errors="coerce"),
        "high": pd.to_numeric(unadj["high"], errors="coerce"),
        "low": pd.to_numeric(unadj["low"], errors="coerce"),
        "close": pd.to_numeric(unadj["close"], errors="coerce"),
        "volume": pd.to_numeric(unadj["volume"], errors="coerce"),
    })
    if adj is not None and not adj.empty:
        adj_close = pd.DataFrame({
            "date": [_as_date(d) for d in adj["date"]],
            "adj_close": pd.to_numeric(adj["close"], errors="coerce"),
        })
        base = base.merge(adj_close, on="date", how="left")
    else:
        base["adj_close"] = float("nan")
    base["source"] = "akshare"
    return (base[list(_OUT_COLS)]
            .sort_values("date", kind="mergesort")
            .reset_index(drop=True))


def _fetch_akshare(ticker: str) -> pd.DataFrame:
    """Two akshare round-trips (raw + qfq) merged on date; empty frame on miss.

    Errors are swallowed at DEBUG: if akshare can't reach its mirror or the
    symbol is unknown to it, we simply have no data — same outcome as
    yfinance returning empty, and the caller's downstream join will skip the
    row rather than crash the batch.
    """
    try:
        with _AK_LOCK:
            unadj = _ak.stock_us_daily(symbol=ticker, adjust="")
            adj = _ak.stock_us_daily(symbol=ticker, adjust="qfq")
    except Exception as e:  # noqa: BLE001
        _LOG.debug("akshare fallback for %r failed: %s", ticker, e)
        return _empty_frame()
    return _normalise_akshare(unadj, adj)


# ── Core ───────────────────────────────────────────────────────────────────
class PriceHistory:
    """Thread-safe, lazily-loaded full-history price cache for one ticker.

    Cheap to construct; the network round-trip is deferred to first
    ``.history`` access and memoised via double-checked locking on a
    per-instance RLock. ``_resolve()`` tries yfinance first and falls back
    to akshare only when yfinance returns nothing — so live tickers pay
    the yfinance path only, and delisted tickers transparently land on
    akshare. The outer ``_YF_LOCK`` / ``_AK_LOCK`` are held only across
    the source-library entry points, leaving the heavy network work
    lock-free so concurrent tickers fetch in parallel.
    """
    __slots__ = ("ticker", "_history", "_lock")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._history: pd.DataFrame | None = None
        self._lock = RLock()

    # -- public -------------------------------------------------------------
    @property
    def history(self) -> pd.DataFrame:
        """Cached full daily price history with the canonical schema."""
        h = self._history
        if h is None:
            with self._lock:
                h = self._history
                if h is None:
                    h = self._resolve()
                    self._history = h
        return h

    # -- resolution ---------------------------------------------------------
    def _resolve(self) -> pd.DataFrame:
        # Consult ticker_overrides.yaml for share-class format mismatches
        # (BFB -> BF.B, BRKB -> BRK.B) and merger successors. The override
        # is tried FIRST through both fetchers; if it yields nothing we
        # fall back to the original ticker. Adding an override never
        # drops data — only adds a fetch attempt.
        override = _ticker_override_for(self.ticker)
        candidates = ([override] if (override and override != self.ticker)
                       else [])
        candidates.append(self.ticker)
        for cand in candidates:
            df = _fetch_yf(cand)
            if not df.empty:
                return df
            _LOG.debug("yfinance empty for %r; trying akshare", cand)
            df = _fetch_akshare(cand)
            if not df.empty:
                return df
        return _empty_frame()

    # -- load ---------------------------------------------------------------
    @_timed
    def load(
        self,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> pd.DataFrame:
        """Return daily bars, optionally filtered to ``[start, end]``.

        ``start`` and ``end`` are inclusive on the bar's trading date.
        """
        df = self.history
        if start is not None:
            df = df[df["date"] >= _as_date(start)]
        if end is not None:
            df = df[df["date"] <= _as_date(end)]
        return df.reset_index(drop=True)

    # -- pit ----------------------------------------------------------------
    @_timed
    def pit(self, as_of: str | date) -> pd.Series | None:
        """Return the latest bar with ``date <= as_of``, or ``None`` if none.

        Useful for "last knowable price as of X" lookups; pairs with the
        ``fetching-financial-notices`` skill for filing-date-aligned joins.
        """
        as_of_d = _as_date(as_of)
        df = self.history
        if df.empty:
            return None
        sub = df[df["date"] <= as_of_d]
        if sub.empty:
            return None
        return sub.iloc[-1].copy()


# ── Functional façade ──────────────────────────────────────────────────────
@lru_cache(maxsize=2048)
def _fetcher(ticker: str) -> PriceHistory:
    return PriceHistory(ticker)


@_validate_ticker
def get_prices(
    ticker: str,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
) -> pd.DataFrame:
    """Return daily price bars for ``ticker``, optionally windowed.

    Parameters
    ----------
    ticker : str
        Ticker symbol (Yahoo-style, e.g. "AAPL"). Case-preserved, stripped.
    start, end : date-like, optional
        Inclusive trading-date window. ``None`` means no bound.

    Returns
    -------
    pandas.DataFrame
        Columns: ``date``, ``open``, ``high``, ``low``, ``close``,
        ``adj_close``, ``volume``, ``source``. Empty frame (correct columns)
        when neither yfinance nor akshare has data for the symbol.
        ``open`` / ``high`` / ``low`` / ``close`` are unadjusted exchange
        prices; ``adj_close`` carries the split-and-dividend adjustment as
        of *now* (not as of any past ``as_of``). ``source`` is
        ``"yfinance"`` or ``"akshare"``.
    """
    return _fetcher(ticker).load(start=start, end=end)


@_validate_ticker
def get_pit_price(
    ticker: str,
    as_of: str | date,
) -> pd.Series | None:
    """Return the latest daily bar for ``ticker`` at or before ``as_of``.

    Parameters
    ----------
    ticker : str
        Ticker symbol.
    as_of : date-like
        Cutoff date (inclusive). The most recent bar with ``date <= as_of``
        is returned.

    Returns
    -------
    pandas.Series, or None
        A single bar (Series indexed by the canonical column set) or
        ``None`` when neither yfinance nor akshare has data for that symbol
        on or before ``as_of``.
    """
    return _fetcher(ticker).pit(as_of)


# ── Batch (multi-ticker) façade ────────────────────────────────────────────
def _clean_tickers(tickers: Iterable[str]) -> list[str]:
    """Dedup + strip + drop empty / non-string entries; sorted for determinism."""
    return sorted({t.strip() for t in tickers
                   if isinstance(t, str) and t.strip()})


def get_prices_batch(
    tickers: Iterable[str],
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    max_workers: int = 8,
) -> pd.DataFrame:
    """Parallel ``get_prices`` over many tickers; long frame with ``ticker``.

    Parameters
    ----------
    tickers : iterable of str
        Ticker symbols. Deduplicated and whitespace-stripped.
    start, end :
        See ``get_prices``.
    max_workers : int
        Parallel ticker fetches (default 8). Lower to 1-2 if you hit
        upstream rate limits.

    Returns
    -------
    pandas.DataFrame
        Long-format frame (one row per ticker × trading date) with
        ``ticker`` as the leading column. Per-row ``source`` indicates
        yfinance vs akshare. Per-ticker failures are logged at DEBUG and
        skipped — the batch never raises on a single bad ticker.
    """
    unique = _clean_tickers(tickers)
    if not unique:
        return pd.DataFrame(columns=list(_BATCH_COLS))
    workers = min(max_workers, len(unique)) or 1
    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="px",
    ) as pool:
        futs = {pool.submit(get_prices, t, start=start, end=end): t
                for t in unique}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                df = fut.result()
            except Exception as e:  # noqa: BLE001
                _LOG.debug("get_prices(%r) failed: %s", t, e)
                continue
            if not df.empty:
                frames.append(df.assign(ticker=t))
    if not frames:
        return pd.DataFrame(columns=list(_BATCH_COLS))
    out = pd.concat(frames, copy=False, ignore_index=True)
    return (out[list(_BATCH_COLS)]
            .sort_values(["ticker", "date"], kind="mergesort")
            .reset_index(drop=True))


def get_pit_price_batch(
    tickers: Iterable[str],
    as_of: str | date,
    *,
    max_workers: int = 8,
) -> pd.DataFrame:
    """Parallel ``get_pit_price`` over many tickers; one row per ticker.

    Parameters
    ----------
    tickers : iterable of str
        Ticker symbols.
    as_of : date-like
        See ``get_pit_price``.
    max_workers : int
        Parallel ticker fetches (default 8).

    Returns
    -------
    pandas.DataFrame
        One row per ticker with the leading ``ticker`` column. Tickers with
        no bar at or before ``as_of`` are dropped silently; per-ticker
        failures are logged at DEBUG.
    """
    unique = _clean_tickers(tickers)
    if not unique:
        return pd.DataFrame(columns=list(_BATCH_COLS))
    workers = min(max_workers, len(unique)) or 1
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="px-pit",
    ) as pool:
        futs = {pool.submit(get_pit_price, t, as_of): t for t in unique}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                _LOG.debug("get_pit_price(%r) failed: %s", t, e)
                continue
            if row is None:
                continue
            rows.append({"ticker": t, **row.to_dict()})
    if not rows:
        return pd.DataFrame(columns=list(_BATCH_COLS))
    return (pd.DataFrame(rows, columns=list(_BATCH_COLS))
            .sort_values("ticker", kind="mergesort")
            .reset_index(drop=True))


# ── CLI ────────────────────────────────────────────────────────────────────
def _write(df: pd.DataFrame, path: str, parser: argparse.ArgumentParser) -> None:
    if path.endswith(".parquet"):
        df.to_parquet(path, index=False)
    elif path.endswith(".csv"):
        df.to_csv(path, index=False)
    else:
        parser.error(f"output path must end in .parquet or .csv, got {path!r}")
    print(f"Wrote {len(df):,} rows to {path}")


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=("Fetch daily price bars (OHLC + adj_close + volume) "
                     "from Yahoo Finance, falling back to akshare for "
                     "delisted tickers.")
    )
    p.add_argument("tickers", nargs="+",
                   help="Ticker symbol(s), e.g. AAPL MSFT NVDA. Multi-ticker "
                        "runs use parallel fetches and produce a long-format "
                        "frame.")
    p.add_argument("--as-of", default=None,
                   help="Single PIT lookup at this date (YYYY-MM-DD). "
                        "Mutually exclusive with --start / --end.")
    p.add_argument("--start", default=None,
                   help="Series start date YYYY-MM-DD (inclusive).")
    p.add_argument("--end", default=None,
                   help="Series end date YYYY-MM-DD (inclusive).")
    p.add_argument("--workers", type=int, default=8,
                   help="Max parallel ticker fetches for multi-ticker runs.")
    p.add_argument("--out", default=None,
                   help="Write results to this path (.parquet or .csv).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Emit DEBUG-level timing logs (also reveals which "
                        "source served each ticker).")
    args = p.parse_args(argv)

    if args.as_of and (args.start or args.end):
        p.error("--as-of is mutually exclusive with --start / --end.")

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    single = len(args.tickers) == 1

    if args.as_of:
        if single:
            row = get_pit_price(args.tickers[0], args.as_of)
            if row is None:
                print(f"{args.tickers[0]}: no bar at or before {args.as_of}")
                return 0
            print(f"{args.tickers[0]} as of {row['date']} ({row['source']}):")
            for col in _OUT_COLS:
                if col in ("date", "source"):
                    continue
                print(f"  {col:<10} {row[col]}")
            return 0
        df = get_pit_price_batch(
            args.tickers, args.as_of, max_workers=args.workers,
        )
    elif single:
        df = get_prices(
            args.tickers[0], start=args.start, end=args.end,
        )
    else:
        df = get_prices_batch(
            args.tickers,
            start=args.start, end=args.end,
            max_workers=args.workers,
        )

    if args.out:
        _write(df, args.out, p)
    else:
        print(df.head().to_string(index=False))
        print(f"\n{len(df):,} rows x {df.shape[1]} columns")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
