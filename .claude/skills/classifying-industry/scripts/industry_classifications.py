"""Industry-classification enrichment for NPORT-P fund holdings.

Joins two authoritative taxonomies onto a holdings frame:
  * **financedatabase** (MSCI-style): `sector`, `industry_group`, `industry`
  * **SEC EDGAR** (regulatory):       `sic`, `sic_industry`

Primary join key is ISIN (present on every NPORT line). Ticker is the
fallback for the fd lookup and the sole key for the SEC lookup.

CLI:
    python industry_classifications.py SPY
    python industry_classifications.py SPY --out spy_industry.parquet
    python industry_classifications.py --from qqq_universe.parquet \\
        --out qqq_industry.parquet

Library:
    from industry_classifications import get_industry_table
    df = get_industry_table("SPY")                 # fetch + enrich
    df = get_industry_table(existing_holdings_df)  # enrich in place
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from threading import RLock
from typing import Iterable

import financedatabase as _fd
import pandas as pd
from edgar import Company, set_identity

# Make the sibling `fund_holdings.py` importable regardless of how this script
# is invoked (CLI, `python -m`, or imported from outside the skill folder).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fund_holdings import get_fund_holdings  # noqa: E402  (after sys.path)

__all__ = [
    "get_industry_table",
    "load_equities_reference",
    "set_identity",
]

_LOG = logging.getLogger(__name__)
_EDGAR_LOCK = RLock()   # serialises edgar's shared global session
_FD_LOCK = RLock()      # serialises the one-shot fd.Equities() build

_FD_COLS = ("name", "sector", "industry_group", "industry")
_OUT_COLS = (
    "ticker", "isin", "name",
    "sector", "industry_group", "industry",
    "sic", "sic_industry",
)


# ── financedatabase reference table ───────────────────────────────────────
@lru_cache(maxsize=1)
def load_equities_reference() -> pd.DataFrame:
    """Full financedatabase equities table, cached for the process lifetime.

    Returned frame is indexed by `symbol` and carries `isin` as a column —
    both are valid join keys. First call is expensive (~160k rows); all
    subsequent calls are free.
    """
    with _FD_LOCK:
        return _fd.Equities().select().copy()


# ── Name-based fd validation ──────────────────────────────────────────────
_NAME_NOISE = re.compile(r"[.,;:\"'()\[\]/]|\bdl-?,?\d*\b", re.IGNORECASE)
_NAME_STOPWORDS = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "plc",
    "ltd", "limited", "llc", "lp", "nv", "sa", "ag", "se", "the",
    "holdings", "holding", "class", "common", "stock", "shares", "ordinary",
    "series", "a", "b", "c", "group", "international", "intl", "trust",
    "new", "old", "american", "depositary",
})


def _name_tokens(s) -> frozenset[str]:
    """Lowercase bag-of-meaningful-tokens for a company name."""
    if not isinstance(s, str) or not s.strip():
        return frozenset()
    cleaned = _NAME_NOISE.sub(" ", s.lower())
    return frozenset(
        t for t in cleaned.split()
        if len(t) >= 2 and t not in _NAME_STOPWORDS and not t.isdigit()
    )


def _validate_fd_by_name(
    enriched: pd.DataFrame, keys: pd.DataFrame, fd_cols: list[str],
) -> pd.DataFrame:
    """Null out fd columns where fd's name disagrees with input `_nport_name`.

    fd occasionally carries a completely different issuer at a ticker
    symbol (e.g. fd[ticker=ZWS] points to Telia Lietuva instead of Zurn
    Elkay). When the caller supplied a name via NPORT, we can catch these
    by checking for any meaningful-token overlap.
    """
    if "_nport_name" not in keys.columns or "name" not in enriched.columns:
        return enriched
    merged = enriched.merge(
        keys[["ticker", "isin", "_nport_name"]],
        on=["ticker", "isin"], how="left",
    )
    fd_tok = merged["name"].apply(_name_tokens)
    np_tok = merged["_nport_name"].apply(_name_tokens)
    no_overlap = pd.Series(
        [not (a & b) for a, b in zip(fd_tok, np_tok)],
        index=merged.index,
    )
    disagree = (
        merged["name"].notna()
        & merged["_nport_name"].notna()
        & fd_tok.map(bool) & np_tok.map(bool)
        & no_overlap
    )
    if disagree.any():
        merged.loc[disagree, list(fd_cols)] = pd.NA
    return merged.drop(columns=["_nport_name"])


def _fd_enrich(keys: pd.DataFrame, equities: pd.DataFrame) -> pd.DataFrame:
    """Attach fd columns to `keys` via a three-stage disambiguating join.

    fd keeps one row per ISIN *per listing venue* (MSFT, MSF.DE, MSFT.MX,
    … all share US5949181045) plus occasional stray rows that reuse a US
    ISIN for an unrelated foreign fund. Match on (ticker, isin) together
    to pin the venue; fall back to ISIN alone, then ticker alone.
    """
    fd_cols = [c for c in _FD_COLS if c in equities.columns]
    if not fd_cols:
        return keys.assign(**{c: pd.NA for c in _FD_COLS})

    flat = equities.reset_index()[["symbol", "isin", *fd_cols]].rename(
        columns={"symbol": "ticker"}
    )
    # Drop fd's placeholder stub rows (name in {"one","two"}) — ~1900
    # default-dumping rows that otherwise poison ticker-only matches.
    if "name" in flat.columns:
        flat = flat[~flat["name"].astype("string").str.lower()
                    .isin({"one", "two"})]

    out = keys.merge(flat, on=["ticker", "isin"], how="left")
    miss = out[fd_cols[0]].isna()

    if miss.any():
        by_isin = (flat.dropna(subset=["isin"])
                   .drop_duplicates("isin", keep="first")
                   .set_index("isin"))
        fill = keys.loc[miss, ["isin"]].merge(
            by_isin[fd_cols], how="left", left_on="isin", right_index=True,
        )
        for c in fd_cols:
            out.loc[miss, c] = fill[c].values
        miss = out[fd_cols[0]].isna()

    if miss.any():
        by_ticker = (flat.drop_duplicates("ticker", keep="first")
                     .set_index("ticker"))
        fill = keys.loc[miss, ["ticker"]].merge(
            by_ticker[fd_cols], how="left", left_on="ticker", right_index=True,
        )
        for c in fd_cols:
            out.loc[miss, c] = fill[c].values

    return out


# ── SEC classification (sic + sic_industry) ───────────────────────────────
@lru_cache(maxsize=16384)
def _sec_classification(ticker: str) -> tuple[str | None, str | None]:
    """Return (sic, sic_industry) for a ticker; (None, None) on any failure.

    `Company.sic` is a 4-digit SIC code; `Company.industry` is the SIC
    industry label (falls back to `sic_description` on older edgar versions).
    """
    if not ticker:
        return (None, None)
    try:
        with _EDGAR_LOCK:
            c = Company(ticker)
        sic = getattr(c, "sic", None)
        ind = (getattr(c, "industry", None)
               or getattr(c, "sic_description", None))
    except Exception as e:  # noqa: BLE001
        _LOG.debug("Company(%r) failed: %s", ticker, e)
        return (None, None)
    return (str(sic) if sic else None, str(ind) if ind else None)


def _sec_batch(tickers: Iterable[str], *, max_workers: int = 8) -> pd.DataFrame:
    """Parallel SEC lookup deduplicated across tickers.

    Returns a frame indexed by `ticker` with columns `sic`, `sic_industry`.
    """
    unique = sorted({t for t in tickers if isinstance(t, str) and t})
    if not unique:
        return pd.DataFrame(
            columns=["sic", "sic_industry"],
            index=pd.Index([], name="ticker"),
        )
    workers = min(max_workers, len(unique)) or 1
    rows: dict[str, tuple[str | None, str | None]] = {}
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="sec-sic",
    ) as pool:
        futs = {pool.submit(_sec_classification, t): t for t in unique}
        for fut in as_completed(futs):
            rows[futs[fut]] = fut.result()
    out = pd.DataFrame.from_dict(
        rows, orient="index", columns=["sic", "sic_industry"],
    )
    out.index.name = "ticker"
    return out


# ── Main facade ───────────────────────────────────────────────────────────
def get_industry_table(
    holdings: pd.DataFrame | str,
    *,
    max_workers: int = 8,
) -> pd.DataFrame:
    """Return industry classifications for each holding.

    Parameters
    ----------
    holdings : DataFrame or str
        Either a frame produced by ``step_1_get_universe.get_fund_holdings``
        (must carry ``isin`` and/or ``ticker_resolved``), or a fund ticker
        string which will be fetched fresh.
    max_workers : int
        Parallelism for SEC ticker lookups.

    Returns
    -------
    pandas.DataFrame
        One row per unique (ticker, isin) pair, with columns:
        ``ticker``, ``isin``, ``name``, ``sector``, ``industry_group``,
        ``industry``, ``sic``, ``sic_industry``.
    """
    if isinstance(holdings, str):
        holdings = get_fund_holdings(holdings)
    if holdings is None or len(holdings) == 0:
        return pd.DataFrame(columns=list(_OUT_COLS))

    # Pick the best ticker column available.
    tk_src = holdings.get("ticker_resolved")
    if tk_src is None:
        tk_src = holdings.get("ticker")
    elif "ticker" in holdings.columns:
        tk_src = tk_src.fillna(holdings["ticker"])
    tk = pd.Series(tk_src, index=holdings.index).astype("string").str.strip()

    isin = holdings.get("isin")
    isin = (pd.Series(isin, index=holdings.index)
            .astype("string").str.strip()) if isin is not None else \
           pd.Series(pd.NA, index=holdings.index, dtype="string")

    data = {"ticker": tk, "isin": isin}
    if "name" in holdings.columns:
        data["_nport_name"] = (
            pd.Series(holdings["name"], index=holdings.index)
            .astype("string").str.strip()
        )
    keys = (pd.DataFrame(data)
            .replace({"": pd.NA})
            .drop_duplicates(subset=["ticker", "isin"])
            .reset_index(drop=True))

    equities = load_equities_reference()
    join_keys = keys.drop(columns=["_nport_name"], errors="ignore")
    enriched = _fd_enrich(join_keys, equities)
    fd_cols = [c for c in _FD_COLS if c in equities.columns]
    enriched = _validate_fd_by_name(enriched, keys, fd_cols)

    sec = _sec_batch(keys["ticker"].dropna().unique(),
                     max_workers=max_workers)
    out = enriched.merge(sec, how="left", left_on="ticker", right_index=True)

    for c in _OUT_COLS:
        if c not in out.columns:
            out[c] = pd.NA
    return out[list(_OUT_COLS)].reset_index(drop=True)


# ── CLI ───────────────────────────────────────────────────────────────────
def _read_holdings(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    if path.endswith(".csv"):
        return pd.read_csv(path)
    raise ValueError(f"--from must be .parquet or .csv, got {path!r}")


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Enrich fund holdings with sector / industry / SIC."
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("ticker", nargs="?",
                     help="Fund ticker to fetch via step_1 (e.g. SPY).")
    src.add_argument("--from", dest="from_path", default=None,
                     help="Load holdings from a .parquet or .csv file.")
    p.add_argument("--identity", default=None,
                   help="SEC contact email. Falls back to $EDGAR_IDENTITY.")
    p.add_argument("--out", default=None,
                   help="Write results to this path (.parquet or .csv).")
    p.add_argument("--workers", type=int, default=8,
                   help="Max parallel SEC lookups (default 8).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Emit DEBUG-level logs.")
    args = p.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    ident = args.identity or os.environ.get("EDGAR_IDENTITY")
    if not ident:
        p.error("Identity required: pass --identity or set EDGAR_IDENTITY.")
    set_identity(ident)

    holdings: pd.DataFrame | str
    if args.from_path:
        holdings = _read_holdings(args.from_path)
    else:
        holdings = args.ticker

    df = get_industry_table(holdings, max_workers=args.workers)

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
