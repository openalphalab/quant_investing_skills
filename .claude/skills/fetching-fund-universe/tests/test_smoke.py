"""Smoke tests for fetching-fund-universe.

Run from this skill's folder:
    pytest tests/

Network-touching tests are skipped unless EDGAR_IDENTITY is set in the
environment. Offline tests (import + input validation) always run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the script importable without installing the skill as a package.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fund_holdings  # noqa: E402

EDGAR_IDENTITY = os.environ.get("EDGAR_IDENTITY")
needs_network = pytest.mark.skipif(
    not EDGAR_IDENTITY,
    reason="set EDGAR_IDENTITY=email to enable network smoke tests",
)

EXPECTED_COLS = {
    "name", "ticker", "isin", "cusip", "value_usd", "pct_value",
    "asset_category", "currency_code", "filling_date", "reporting_date",
}


def test_module_imports():
    """The module loads cleanly with no side effects."""
    assert hasattr(fund_holdings, "get_fund_holdings")
    assert hasattr(fund_holdings, "FundHoldings")
    assert hasattr(fund_holdings, "set_identity")


@pytest.mark.parametrize("bad", ["", "   ", None, 123, 0])
def test_validate_ticker_rejects_bad_input(bad):
    """Empty / non-string tickers raise before any network I/O."""
    with pytest.raises(ValueError, match="ticker must be a non-empty str"):
        fund_holdings.get_fund_holdings(bad)


@needs_network
def test_fetch_spy_latest_filing():
    """The most recent SPY filing returns ~500 rows with the expected schema."""
    fund_holdings.set_identity(EDGAR_IDENTITY)
    fh = fund_holdings.FundHoldings("SPY")
    assert fh.filings, "no NPORT-P filings discovered for SPY"
    latest = max(ref.filing_date for ref in fh.filings)
    df = fh.load(dates=[latest], resolve_tickers=False)
    assert len(df) > 100, f"expected >100 holdings, got {len(df)}"
    assert EXPECTED_COLS.issubset(df.columns), (
        f"missing columns: {EXPECTED_COLS - set(df.columns)}"
    )
    assert df["reporting_date"].nunique() == 1


@needs_network
def test_fetch_with_ticker_resolution():
    """resolve_tickers=True populates ticker_resolved when enabled."""
    fund_holdings.set_identity(EDGAR_IDENTITY)
    fh = fund_holdings.FundHoldings("SPY")
    latest = max(ref.filing_date for ref in fh.filings)
    df = fh.load(dates=[latest], resolve_tickers=True)
    assert "ticker_resolved" in df.columns
    assert df["ticker_resolved"].notna().sum() > 0
