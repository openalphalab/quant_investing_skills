"""Smoke tests for classifying-industry.

Run from this skill's folder:
    pytest tests/

Network-touching tests are skipped unless EDGAR_IDENTITY is set in the
environment. Offline tests (import + sibling-vendor wiring) always run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import industry_classifications  # noqa: E402
import fund_holdings  # noqa: E402  (vendored sibling, surfaced via sys.path bootstrap)

EDGAR_IDENTITY = os.environ.get("EDGAR_IDENTITY")
needs_network = pytest.mark.skipif(
    not EDGAR_IDENTITY,
    reason="set EDGAR_IDENTITY=email to enable network smoke tests",
)

EXPECTED_COLS = [
    "ticker", "isin", "name",
    "sector", "industry_group", "industry",
    "sic", "sic_industry",
]


def test_module_imports():
    """Both this module and its vendored fund_holdings sibling are importable."""
    assert hasattr(industry_classifications, "get_industry_table")
    assert hasattr(industry_classifications, "load_equities_reference")
    assert hasattr(fund_holdings, "get_fund_holdings")


def test_vendored_fund_holdings_has_banner():
    """The vendored copy carries the sync banner so editors know not to touch it."""
    text = (SCRIPTS / "fund_holdings.py").read_text(encoding="utf-8")
    assert text.startswith("# >>> VENDORED — DO NOT EDIT >>>"), (
        "vendored fund_holdings.py is missing its banner; "
        "run `python tools/sync_vendored.py` from the repo root"
    )


def test_get_industry_table_empty_input():
    """Empty input returns an empty frame with the expected schema."""
    out = industry_classifications.get_industry_table(pd.DataFrame())
    assert list(out.columns) == EXPECTED_COLS
    assert len(out) == 0


@needs_network
def test_enrich_minimal_holdings():
    """A 2-row holdings frame round-trips through fd + SEC enrichment."""
    industry_classifications.set_identity(EDGAR_IDENTITY)
    holdings = pd.DataFrame({
        "ticker_resolved": ["NVDA", "AAPL"],
        "isin": ["US67066G1040", "US0378331005"],
        "name": ["NVIDIA Corp", "Apple Inc"],
    })
    out = industry_classifications.get_industry_table(holdings)
    assert list(out.columns) == EXPECTED_COLS
    assert len(out) == 2
    # fd should resolve sector for both — they're large-cap US equities.
    assert out["sector"].notna().all(), out
    # SEC SIC should resolve at least one.
    assert out["sic"].notna().any(), out
