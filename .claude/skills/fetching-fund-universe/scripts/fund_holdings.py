"""Reusable NPORT-P fund holdings fetcher.

Thread-safe, memory-efficient, zero side-effects on import.

CLI:
    python fund_holdings.py SPY --identity me@example.com
    python fund_holdings.py SPY --dates 2024-03-31 2024-06-30 --out spy.parquet
    EDGAR_IDENTITY=me@example.com python fund_holdings.py QQQ

Library:
    from fund_holdings import get_fund_holdings, set_identity
    set_identity("me@example.com")
    df = get_fund_holdings("SPY", dates=["2024-03-31"])
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from functools import lru_cache, wraps
from threading import RLock
from typing import Callable, Iterable, NamedTuple

import httpx
import pandas as pd
from edgar import Company, find as _edgar_find, set_identity

try:  # series resolution for umbrella-trust ETFs (iShares, Vanguard, Invesco...)
    from edgar.funds import find_fund
except ImportError:  # pragma: no cover
    find_fund = None

try:  # yfinance powers the ISIN -> ticker fallback; dependency is optional
    import yfinance as _yf
except ImportError:  # pragma: no cover
    _yf = None

__all__ = ["FundHoldings", "get_fund_holdings", "set_identity"]

_LOG = logging.getLogger(__name__)
_EDGAR_LOCK = RLock()  # serialises edgar's shared global session
_EDGAR_BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


class _Ref(NamedTuple):
    """Lightweight filing reference: metadata only, body fetched on demand."""
    accession_no: str
    filing_date: date


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


# ── ISIN -> ticker fallback ────────────────────────────────────────────────
@lru_cache(maxsize=8192)
def _isin_to_yf_ticker(isin: str) -> str | None:
    """Resolve an ISIN to its Yahoo Finance symbol via `yfinance.Search`.

    Yahoo's first quote for a US-issuer ISIN is sometimes a non-US listing
    (e.g. "LRCX.MX" for Lam Research). We prefer the unsuffixed (US) symbol
    when one is present in the result set, so EDGAR XBRL lookups downstream
    don't break on the foreign secondary listing.

    Returns ``None`` on miss or transport error. Cached for the process
    lifetime; safe to call from many threads.
    """
    if _yf is None or not isin:
        return None
    try:
        quotes = _yf.Search(isin).quotes or []
    except Exception as e:  # noqa: BLE001
        _LOG.debug("yfinance.Search(%r) failed: %s", isin, e)
        return None
    if not quotes:
        return None
    # Prefer a quote whose symbol has no exchange suffix — that's the US
    # listing in Yahoo's convention (US tickers are bare; non-US carry
    # `.MX`, `.TO`, `.L`, etc.).
    for q in quotes:
        sym = (q.get("symbol") or "").strip()
        if sym and "." not in sym:
            return sym
    # All matches are non-US — strip the suffix off the first one as a
    # last-resort fallback. The bare prefix is usually the US ticker
    # (e.g. "LRCX.MX" -> "LRCX") for ADR / cross-listed issuers.
    sym = (quotes[0].get("symbol") or "").strip()
    base = sym.split(".", 1)[0]
    return base or None


# 4+ trailing X's flag NPORT-P right-padding ("LRCXXXXX", "SLGXXXX",
# "BLKXXXX"). Real US tickers don't have 4 consecutive trailing X's; IDXX
# (IDEXX Labs) has only 2 and is correctly treated as a real ticker.
_PADDED_TICKER_RE = re.compile(r"X{4,}$")

# Real US equity tickers are A-Z plus an optional single dot/dash share-class
# suffix (e.g. AAPL, MSFT, BRK.B, BF-B). NPORT pseudo-tickers built from a
# CUSIP-suffix or paired-off code (e.g. "F104PAIROFF" for Discovery Inc,
# "L100PS" for Arconic) contain digits and don't match this shape — those
# get routed through ISIN -> yfinance the same way blank tickers do.
_VALID_TICKER_RE = re.compile(r"^[A-Z]+([.-][A-Z]+)?$")


def _looks_padded(t: str | None) -> bool:
    """True iff the NPORT `ticker` field is padded with 4+ trailing X's.

    Some filers (State Street's SPY among them) right-pad the ticker column
    with X's. The padded value won't resolve in EDGAR, so we treat it as
    "unresolved" and fall back to ISIN -> yfinance.
    """
    return isinstance(t, str) and bool(_PADDED_TICKER_RE.search(t))


def _looks_synthetic(t: str | None) -> bool:
    """True iff the NPORT `ticker` doesn't look like a real US equity ticker.

    Catches CUSIP-suffix pseudo-tickers ("F104PAIROFF" = Discovery Inc by
    CUSIP `25470F104`; "L100PS" = Arconic by CUSIP `03965L100`), digit-
    bearing transaction codes, and anything else that fails the
    A-Z-plus-optional-share-class shape check. These should also be routed
    through ISIN -> yfinance; the ISIN field on the same NPORT row is
    correct and yfinance returns the canonical ticker for it.

    A blank / NA ticker is NOT synthetic — the caller already handles that
    case separately.
    """
    if not isinstance(t, str):
        return False
    s = t.strip().upper()
    if not s:
        return False
    return not bool(_VALID_TICKER_RE.match(s))


def _resolve_ticker_column(
    df: pd.DataFrame, *, max_workers: int = 8,
) -> pd.Series:
    """Return a Series aligned to `df.index` combining NPORT `ticker` with a
    yfinance-by-ISIN fallback.

    Rows treated as "unresolved" (and routed through the ISIN lookup):
      * `ticker` is NA / blank.
      * `ticker` is X-padded by the filer (>=4 trailing X's, e.g.
        "LRCXXXXX", "SLGXXXX", "BLKXXXX") — won't resolve in EDGAR.
      * `ticker` is a NPORT pseudo-ticker built from CUSIP suffix or a
        transaction code (e.g. "F104PAIROFF" = Discovery Inc by CUSIP,
        "L100PS" = Arconic). Detected via the `_looks_synthetic` shape
        check — anything outside `[A-Z]+([.-][A-Z]+)?` is suspect.

    Unique ISINs are looked up once in parallel; `lru_cache` deduplicates
    repeat calls across invocations.
    """
    if "ticker" not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="string")
    s = df["ticker"].astype("string")
    blank = s.isna() | (s.str.strip() == "")
    padded = s.fillna("").map(_looks_padded).astype(bool)
    synthetic = s.fillna("").map(_looks_synthetic).astype(bool)
    missing = blank | padded | synthetic
    if "isin" not in df.columns or not missing.any():
        return s
    isin = df["isin"].astype("string").str.strip()
    needs = isin[missing & isin.notna() & (isin != "")]
    unique_isins = sorted(set(needs))
    if unique_isins:
        workers = min(max_workers, len(unique_isins)) or 1
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="yf-isin",
        ) as pool:
            for _ in as_completed(
                {pool.submit(_isin_to_yf_ticker, x) for x in unique_isins}
            ):
                pass
    resolved = needs.map(_isin_to_yf_ticker).astype("string")
    return s.mask(missing, resolved)


# ── Core ───────────────────────────────────────────────────────────────────
class FundHoldings:
    """Thread-safe, lazily-loaded NPORT-P holdings fetcher.

    Cheap to construct; all network I/O is deferred to `.load()`.

    Two resolution paths — transparent to the caller:
      * **Standalone trust** (e.g. SPY) — `Company(ticker).get_filings("NPORT-P")`
      * **Series within an umbrella trust** (e.g. IWM in iShares Trust, VOO in
        Vanguard Index Funds) — SEC's series-scoped atom feed at CIK=S000xxx,
        materialising each filing lazily via accession number.

    Double-checked locking on the per-instance RLock keeps the hot read path
    lock-free after the first resolution. `_EDGAR_LOCK` serialises the one-shot
    call into edgar's shared global session during resolution.
    """
    __slots__ = ("ticker", "_refs", "_series_id", "_lock")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._refs: list[_Ref] | None = None
        self._series_id: str | None = None
        self._lock = RLock()

    # -- public -------------------------------------------------------------
    @property
    def series_id(self) -> str | None:
        """Series ID if this ticker is a series within an umbrella trust."""
        _ = self.filings  # force resolution
        return self._series_id

    @property
    def filings(self) -> list[_Ref]:
        """Cached list of filing references (accession_no + filing_date)."""
        r = self._refs
        if r is None:
            with self._lock:
                r = self._refs
                if r is None:
                    with _EDGAR_LOCK:
                        r = self._resolve()
                    self._refs = r
        return r

    # -- resolution ---------------------------------------------------------
    def _resolve(self) -> list[_Ref]:
        sid = self._lookup_series_id()
        self._series_id = sid
        if sid is not None:
            return _atom_refs(sid)
        # Standalone trust path (SPY-style).
        filings = Company(self.ticker).get_filings(form="NPORT-P")
        return [_Ref(f.accession_no, _as_date(f.filing_date)) for f in filings]

    def _lookup_series_id(self) -> str | None:
        if find_fund is None:
            return None
        try:
            fc = find_fund(self.ticker)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("find_fund(%s) failed: %s", self.ticker, e)
            return None
        series = getattr(fc, "series", None)
        return series.series_id if series is not None else None

    # -- extraction ---------------------------------------------------------
    @staticmethod
    def _extract(ref: _Ref) -> pd.DataFrame:
        """Materialise a filing by accession number, return holdings + dates."""
        filing = _edgar_find(ref.accession_no)
        info = filing.obj()
        return info.investment_data().assign(
            filling_date=info.filing.filing_date,
            reporting_date=info.reporting_period,
        )

    # -- load ---------------------------------------------------------------
    @_timed
    def load(
        self,
        dates: Iterable | None = None,
        *,
        max_workers: int = 8,
        resolve_tickers: bool = True,
    ) -> pd.DataFrame:
        """Return holdings DataFrame, optionally filtered by filing date.

        When `resolve_tickers` is true, adds a `ticker_resolved` column: the
        NPORT `ticker` when present, else a yfinance-by-ISIN lookup. Requires
        the optional `yfinance` package; silently no-ops if unavailable.
        """
        refs = self.filings
        selected = self._select(refs, dates)
        if not selected:
            return pd.DataFrame()
        frames: list[pd.DataFrame | None] = [None] * len(selected)
        workers = min(max_workers, len(selected)) or 1
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="nport") as pool:
            futs = {pool.submit(self._extract, ref): p
                    for p, ref in enumerate(selected)}
            for fut in as_completed(futs):
                frames[futs[fut]] = fut.result()
        df = pd.concat(frames, copy=False, ignore_index=True)
        if resolve_tickers and not df.empty:
            df["ticker_resolved"] = _resolve_ticker_column(
                df, max_workers=max_workers,
            )
        return df

    @staticmethod
    def _select(refs: list[_Ref], dates: Iterable | None) -> list[_Ref]:
        if dates is None:
            return list(refs)
        wanted = frozenset(map(_as_date, dates))
        return [r for r in refs if r.filing_date in wanted]


# ── Atom feed helper ──────────────────────────────────────────────────────
def _atom_refs(series_id: str, *, page_size: int = 100) -> list[_Ref]:
    """Return filing refs for `series_id` via SEC EDGAR's atom browse feed.

    Handles pagination by walking `&start=` offsets until a page returns fewer
    than `page_size` entries.
    """
    ua = os.environ.get("EDGAR_IDENTITY") or "openalphalab@gmail.com"
    out: list[_Ref] = []
    start = 0
    with httpx.Client(headers={"User-Agent": ua}, timeout=30.0) as client:
        while True:
            r = client.get(_EDGAR_BROWSE, params={
                "action": "getcompany", "CIK": series_id, "type": "NPORT-P",
                "dateb": "", "owner": "include", "count": page_size,
                "start": start, "output": "atom",
            })
            r.raise_for_status()
            root = ET.fromstring(r.content)
            entries = root.findall("a:entry", _ATOM_NS)
            if not entries:
                break
            for e in entries:
                acc = e.findtext("a:content/a:accession-number", namespaces=_ATOM_NS)
                fdate = e.findtext("a:content/a:filing-date", namespaces=_ATOM_NS)
                if acc and fdate:
                    out.append(_Ref(acc.strip(), _as_date(fdate.strip())))
            if len(entries) < page_size:
                break
            start += page_size
    return out


# ── Functional façade ──────────────────────────────────────────────────────
@lru_cache(maxsize=32)
def _fetcher(ticker: str) -> FundHoldings:
    return FundHoldings(ticker)


@_validate_ticker
def get_fund_holdings(
    ticker: str,
    dates: Iterable | None = None,
    *,
    max_workers: int = 8,
    resolve_tickers: bool = True,
) -> pd.DataFrame:
    """Return NPORT-P holdings for `ticker`, optionally filtered by filing date.

    Parameters
    ----------
    ticker : str
        Fund ticker, e.g. "SPY", "QQQ". Case-preserved, stripped.
    dates : iterable of date-like, optional
        Filing dates to include. Accepts str | date | datetime | Timestamp.
        `None` returns every filing.
    max_workers : int
        Upper bound on concurrent filing downloads. Lower it (1-2) if you hit
        EDGAR rate limits. Also caps concurrent yfinance ISIN lookups.
    resolve_tickers : bool
        When true (default), add a `ticker_resolved` column: the NPORT `ticker`
        when present, else a yfinance-by-ISIN fallback (`yf.Search(isin)`).
        Set to false to skip the Yahoo lookups.

    Returns
    -------
    pandas.DataFrame
        Investment rows from every matching filing, plus `filling_date`,
        `reporting_date`, and (when enabled) `ticker_resolved` columns.
    """
    return _fetcher(ticker).load(
        dates, max_workers=max_workers, resolve_tickers=resolve_tickers,
    )


# ── CLI ────────────────────────────────────────────────────────────────────
def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Fetch NPORT-P fund holdings for a ticker from SEC EDGAR."
    )
    p.add_argument("ticker", help="Fund ticker, e.g. SPY")
    p.add_argument("--dates", nargs="*", default=None,
                   help="Filing dates YYYY-MM-DD. Omit for all filings.")
    p.add_argument("--identity", default=None,
                   help="SEC contact email. Falls back to $EDGAR_IDENTITY.")
    p.add_argument("--out", default=None,
                   help="Write results to this path (.parquet or .csv).")
    p.add_argument("--workers", type=int, default=8,
                   help="Max parallel filing downloads (default 8).")
    p.add_argument("--no-resolve-tickers", dest="resolve_tickers",
                   action="store_false",
                   help="Skip the yfinance ISIN->ticker fallback column.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Emit DEBUG-level timing logs.")
    args = p.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    ident = args.identity or os.environ.get("EDGAR_IDENTITY")
    if not ident:
        p.error("Identity required: pass --identity or set EDGAR_IDENTITY.")
    set_identity(ident)

    df = get_fund_holdings(
        args.ticker, args.dates,
        max_workers=args.workers,
        resolve_tickers=args.resolve_tickers,
    )

    if args.out:
        out = args.out
        if out.endswith(".parquet"):
            df.to_parquet(out, index=False)
        elif out.endswith(".csv"):
            df.to_csv(out, index=False)
        else:
            p.error("--out must end in .parquet or .csv")
        print(f"Wrote {len(df):,} rows to {out}")
    else:
        print(df.head().to_string())
        print(f"\n{len(df):,} rows x {df.shape[1]} columns")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
