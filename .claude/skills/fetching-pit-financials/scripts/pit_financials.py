"""Point-in-time financial-fact retrieval (TTM flows + balance-sheet snapshots).

For each (issuer, concept), pull the value as it was knowable on a given date.
Auto-routes by XBRL ``period_type``: ``duration`` concepts (income / cashflow
flows) get TTM aggregation; ``instant`` concepts (balance sheet) get the latest
reported snapshot. Filing-date filter blocks look-ahead via later amendments.

CLI:
    python pit_financials.py AAPL NetIncomeLoss
    python pit_financials.py AAPL NetIncomeLoss --start 2018-01-01 --end 2025-12-31
    python pit_financials.py AAPL Assets --as-of 2024-06-30
    python pit_financials.py AAPL MSFT NVDA GOOGL Assets --as-of 2024-06-30
    python pit_financials.py AAPL MSFT NVDA Revenues --start 2020-01-01 --workers 8 --out rev.parquet

Library:
    from pit_financials import (
        get_pit_value, get_pit_value_batch,
        get_pit_series, get_pit_series_batch,
        set_identity,
    )
    set_identity("me@example.com")
    metric = get_pit_value("AAPL", "NetIncomeLoss", as_of="2024-06-30")
    ts = get_pit_series("AAPL", "Assets", start="2023-01-01")
    snap = get_pit_value_batch(["AAPL","MSFT","NVDA"], "Assets", as_of="2024-06-30")
    long = get_pit_series_batch(["AAPL","MSFT"], "NetIncomeLoss", start="2020-01-01")
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

import pandas as pd
from edgar import Company, set_identity
from edgar.ttm import (
    TTMCalculator,
    TTMMetric,
    apply_split_adjustments,
    detect_splits,
)

__all__ = [
    "FinancialFacts",
    "get_pit_value",
    "get_pit_value_batch",
    "get_pit_series",
    "get_pit_series_batch",
    "set_identity",
]

_LOG = logging.getLogger(__name__)
_EDGAR_LOCK = RLock()  # serialises edgar's shared global session

_TS_COLS: tuple[str, ...] = (
    "observation_date", "period_end", "concept", "label",
    "value", "unit", "periods", "has_gaps", "has_calculated_q4", "warning",
)
_AUDIT_COLS: tuple[str, ...] = (
    "observation_date", "period_end", "value",
    "source_number", "source_concept", "source_label", "source_value",
    "source_unit", "source_period_start", "source_period_end",
    "source_fiscal_year", "source_fiscal_period", "source_filing_date",
    "source_form_type", "source_accession", "calculation_context",
)


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


def _validate_concept(fn: Callable) -> Callable:
    """Reject empty / non-string concepts; complements ``_validate_ticker``."""
    @wraps(fn)
    def _w(ticker, concept, *a, **kw):
        if not isinstance(concept, str) or not concept.strip():
            raise ValueError(f"concept must be a non-empty str, got {concept!r}")
        return fn(ticker, concept.strip(), *a, **kw)
    return _w


# ── Helpers ────────────────────────────────────────────────────────────────
def _as_date(x) -> date:
    """Coerce str | datetime | date | pandas.Timestamp -> datetime.date."""
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return pd.Timestamp(x).date()


def _concept_candidates(concept: str) -> tuple[str, ...]:
    """Mirror edgar's TTM lookup: raw, us-gaap-prefixed, ifrs-prefixed."""
    if ":" in concept:
        return (concept,)
    return (concept, f"us-gaap:{concept}", f"ifrs-full:{concept}")


def _safe(obj: Any, attr: str, default=None):
    """``getattr`` with a default — tolerates the various FinancialFact shapes."""
    return getattr(obj, attr, default)


def _infer_fy_end_month(facts: list, candidate_concepts: tuple[str, ...]) -> int | None:
    """Infer the issuer's fiscal-year-end month from its FY facts.

    Looks at FY-labeled facts among the target concepts that pass the basic
    span check (~365d) and returns the modal `period_end.month`. When the
    issuer's FY-end varies (which happens for off-cycle transition years),
    the modal value still picks the long-run anchor.

    Returns `None` when there's no usable FY anchor — caller should then
    skip the cross-FY-tag validation and rely on span-only checks.
    """
    months: dict[int, int] = {}
    for f in facts:
        if _safe(f, "concept") not in candidate_concepts:
            continue
        if _safe(f, "fiscal_period") != "FY":
            continue
        ps = _safe(f, "period_start")
        pe = _safe(f, "period_end")
        if ps is None or pe is None:
            continue
        try:
            span = (_as_date(pe) - _as_date(ps)).days + 1
        except Exception:  # noqa: BLE001
            continue
        if not 340 <= span <= 380:
            continue
        m = _as_date(pe).month
        months[m] = months.get(m, 0) + 1
    if not months:
        return None
    # Modal month (highest count, tie-break: larger month index for
    # stability — most issuers are calendar-FY = Dec).
    return max(months.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _canonical_fy_fp(
    period_end_raw, span_days: int, fy_end_month: int,
) -> tuple[int, str] | None:
    """Compute the canonical `(fiscal_year, fiscal_period)` labels for a
    duration fact based on its `period_end`, span, and the issuer's
    FY-end month.

    Returns None if the period can't be classified into a standard quarter
    or YTD shape (off-cycle reporting, mid-year transitions, etc.).

    Mapping:
      - span ~90d : single quarter at offset 0/90/180/270 days before FY-end
                     → Q4 / Q3 / Q2 / Q1
      - span ~180d: H1 YTD ending FY_end−180d → Q2 (YTD shape)
      - span ~270d: 9M YTD ending FY_end−90d  → Q3 (YTD shape)
      - span ~365d: full year ending FY_end   → FY
    """
    from calendar import monthrange
    try:
        pe = _as_date(period_end_raw)
    except Exception:  # noqa: BLE001
        return None

    # Fiscal year that CONTAINS pe: if pe.month <= fy_end_month, pe falls
    # in the FY ending in pe.year; else in the next FY.
    fy = pe.year if pe.month <= fy_end_month else pe.year + 1
    try:
        fy_end = date(fy, fy_end_month, monthrange(fy, fy_end_month)[1])
    except ValueError:
        return None
    days_to_fy_end = (fy_end - pe).days

    TOL = 31  # covers off-cycle / 53-week / leap day variations

    # Single-quarter fact (~90 days)
    if 70 <= span_days <= 100:
        for offset, fp in [(0, "Q4"), (90, "Q3"), (180, "Q2"), (270, "Q1")]:
            if abs(days_to_fy_end - offset) <= TOL:
                return fy, fp
        return None
    # H1 YTD (~180 days) → Q2 with YTD shape
    if 160 <= span_days <= 200 and abs(days_to_fy_end - 180) <= TOL:
        return fy, "Q2"
    # 9M YTD (~270 days) → Q3 with YTD shape
    if 250 <= span_days <= 290 and abs(days_to_fy_end - 90) <= TOL:
        return fy, "Q3"
    # Full FY span (~365 days)
    if 340 <= span_days <= 380 and abs(days_to_fy_end) <= TOL:
        return fy, "FY"
    return None


def _augment_with_synthesized_q4s(target: list, fy_end_month: int | None) -> list:
    """Fallback #1: synthesize Q4 facts from FY − (Q1 + Q2 + Q3).

    When TTMCalculator's only Q4-derivation path (Q4 = FY − YTD9M) fails
    because the issuer doesn't tag a YTD9M fact for this concept (or
    tagged it cross-fiscal-year), we can still derive Q4 from the
    individually-reported single-quarter facts using the same algebra:

        Q4 = FY − (Q1 + Q2 + Q3)

    This is a strict algebraic identity. The synthesized Q4 fact carries:
      * `fiscal_year` = the FY's fiscal_year
      * `fiscal_period` = "Q4"
      * `period_end` = FY's period_end
      * `period_start` = Q3's period_end + 1 day
      * `numeric_value` = FY.value − Q1.value − Q2.value − Q3.value
      * `filing_date` = max(filing_dates of the four inputs) — Q4 only
        becomes "knowable" once all four parts are filed (typically the FY
        10-K date).

    PIT safety: callers pass `target` that's already pre-filtered to
    `filing_date <= as_of`, so all four inputs are PIT-knowable. The
    synthesized fact's filing_date is the latest of the four, ensuring
    the upstream filter remains valid.
    """
    if fy_end_month is None:
        return target
    from dataclasses import replace
    from datetime import timedelta

    # Index single-quarter and FY facts by (fiscal_year, fiscal_period).
    # Single-quarter = ~90d span, FY = ~365d span. Drop YTD-shaped facts
    # since this fallback uses singles only.
    by_fy_fp: dict[tuple[int, str], list] = {}
    for f in target:
        fy = _safe(f, "fiscal_year")
        fp = _safe(f, "fiscal_period")
        ps = _safe(f, "period_start")
        pe = _safe(f, "period_end")
        if fy is None or fp is None or ps is None or pe is None:
            continue
        try:
            span = (_as_date(pe) - _as_date(ps)).days + 1
            fy_int = int(fy)
        except Exception:  # noqa: BLE001
            continue
        if fp == "FY" and 340 <= span <= 380:
            by_fy_fp.setdefault((fy_int, "FY"), []).append(f)
        elif fp in ("Q1", "Q2", "Q3", "Q4") and 70 <= span <= 100:
            by_fy_fp.setdefault((fy_int, fp), []).append(f)

    augmented = list(target)
    seen_synth: set[tuple[int, str]] = set()
    for (fy_year, fp) in list(by_fy_fp.keys()):
        if fp != "FY":
            continue
        if (fy_year, "Q4") in by_fy_fp:
            continue   # Q4 already reported as a single — no need to synthesize
        if not all((fy_year, q) in by_fy_fp for q in ("Q1", "Q2", "Q3")):
            continue
        if (fy_year, "Q4") in seen_synth:
            continue

        # Pick the latest-filed fact for each (handles restatements).
        def _latest(fs):
            return max(
                fs,
                key=lambda x: (_as_date(_safe(x, "filing_date")) if _safe(x, "filing_date") else date.min),
            )
        fy_f = _latest(by_fy_fp[(fy_year, "FY")])
        q1_f = _latest(by_fy_fp[(fy_year, "Q1")])
        q2_f = _latest(by_fy_fp[(fy_year, "Q2")])
        q3_f = _latest(by_fy_fp[(fy_year, "Q3")])

        v_fy = _safe(fy_f, "numeric_value")
        v_q1 = _safe(q1_f, "numeric_value")
        v_q2 = _safe(q2_f, "numeric_value")
        v_q3 = _safe(q3_f, "numeric_value")
        if any(v is None for v in (v_fy, v_q1, v_q2, v_q3)):
            continue

        try:
            q3_pe = _as_date(_safe(q3_f, "period_end"))
            fy_pe = _as_date(_safe(fy_f, "period_end"))
        except Exception:  # noqa: BLE001
            continue
        q4_ps = q3_pe + timedelta(days=1)
        # PIT correctness: the synthesized Q4 is knowable only once ALL
        # four constituent filings have been filed.
        filing_dates = []
        for x in (fy_f, q1_f, q2_f, q3_f):
            fd = _safe(x, "filing_date")
            if fd is not None:
                try:
                    filing_dates.append(_as_date(fd))
                except Exception:  # noqa: BLE001
                    pass
        synth_filing = max(filing_dates) if filing_dates else None

        try:
            kwargs = {
                "fiscal_period": "Q4",
                "numeric_value": float(v_fy) - float(v_q1) - float(v_q2) - float(v_q3),
                "period_start": q4_ps,
                "period_end": fy_pe,
            }
            if synth_filing is not None:
                kwargs["filing_date"] = synth_filing
            synth = replace(fy_f, **kwargs)
            augmented.append(synth)
            seen_synth.add((fy_year, "Q4"))
        except (TypeError, ValueError):
            # If `replace` rejects any of these field names, skip the
            # synthesis for this fy. Better to fall through to other
            # fallbacks than emit a malformed fact.
            continue

    return augmented


def _try_fy_anchor(target: list, as_of: date, concept: str) -> "TTMMetric | None":
    """Fallback #2: when as_of falls within ±30 days of an FY's period_end
    AND that FY fact is in target, return it as the TTM directly.

    Math: TTM ending FY-end == FY value (by definition).

    PIT safety: target is pre-filtered to filing_date <= as_of, so the FY
    fact's filing date precedes as_of. The 30-day proximity window means
    we only short-circuit when the as_of really is "at FY-end" — outside
    that window, the FY-as-TTM substitution becomes stale and we don't
    apply it (let the calling code fall through to fallback #3 or null).
    """
    best = None
    best_diff = 31
    best_filing = None
    for f in target:
        if _safe(f, "fiscal_period") != "FY":
            continue
        ps = _safe(f, "period_start")
        pe_raw = _safe(f, "period_end")
        if ps is None or pe_raw is None:
            continue
        try:
            pe = _as_date(pe_raw)
            span = (pe - _as_date(ps)).days + 1
        except Exception:  # noqa: BLE001
            continue
        if not 340 <= span <= 380:
            continue
        diff = abs((pe - as_of).days)
        if diff > best_diff:
            continue
        # Tie-break amendments: when two FY facts share the same period_end
        # (original + 10-K/A), prefer the more recently filed version. This
        # mirrors TTMCalculator._deduplicate_by_period_end's behavior.
        fd = _safe(f, "filing_date")
        try:
            fd_d = _as_date(fd) if fd is not None else None
        except Exception:  # noqa: BLE001
            fd_d = None
        if diff < best_diff:
            best, best_diff, best_filing = f, diff, fd_d
        elif diff == best_diff:
            if best_filing is None or (fd_d is not None and fd_d > best_filing):
                best, best_filing = f, fd_d
    if best is None:
        return None

    return TTMMetric(
        concept=_safe(best, "concept") or concept,
        label=_safe(best, "label"),
        value=_safe(best, "numeric_value"),
        unit=_safe(best, "unit"),
        as_of_date=_as_date(_safe(best, "period_end")),
        periods=[(int(_safe(best, "fiscal_year")), "FY")]
                 if _safe(best, "fiscal_year") is not None else [],
        period_facts=[best],
        has_gaps=False,
        has_calculated_q4=False,
        warning=(f"TTM via FY-anchor fallback (as_of within "
                 f"{best_diff}d of FY-end {_as_date(_safe(best, 'period_end'))})"),
    )


def _try_ytd_plus_prior_tail(
    target: list, as_of: date, fy_end_month: int | None, concept: str,
) -> "TTMMetric | None":
    """Fallback #3: when target has YTD-current + FY-prior + YTD-prior-same-
    position, decompose:

        TTM(as_of) = YTD-current + (FY-prior − YTD-prior-same-position)

    where "YTD-current" is the current fiscal year's most recent YTD slice
    (Q1 ≡ YTD3M, YTD6M, YTD9M, or FY itself), and YTD-prior is the SAME
    position in the prior fiscal year.

    Examples:
      * as_of = mid Q3-2025 → TTM = YTD6M-2025 + (FY-2024 − YTD6M-2024)
        i.e. (first 6 months of 2025) + (last 6 months of 2024)
      * as_of = mid Q4-2025 → TTM = YTD9M-2025 + (FY-2024 − YTD9M-2024)
        i.e. (first 9 months of 2025) + (last 3 months of 2024)

    Math: a strict algebraic decomposition. Same value as a "real" TTM
    when the components reconcile.

    PIT safety: every fact must already be in `target` (pre-filtered to
    filing_date <= as_of), so the decomposition only uses knowable data.
    """
    if fy_end_month is None:
        return None

    # Bucket target by (fiscal_year, "Q1"|"Q2"|"Q3"|"FY") with TYPE:
    # singles (~90d span) act as "Q1 (= YTD3M for first quarter)";
    # YTD shapes ("Q2" with ~180d, "Q3" with ~270d) are the cumulative
    # YTDs we need; FY is the full year. We pick the LATEST-FILED fact
    # for each slot.
    by_fy_slot: dict[tuple[int, str], list] = {}
    for f in target:
        fy = _safe(f, "fiscal_year")
        fp = _safe(f, "fiscal_period")
        ps = _safe(f, "period_start")
        pe = _safe(f, "period_end")
        if fy is None or fp is None or ps is None or pe is None:
            continue
        try:
            span = (_as_date(pe) - _as_date(ps)).days + 1
            fy_int = int(fy)
        except Exception:  # noqa: BLE001
            continue
        slot = None
        if fp == "Q1" and 70 <= span <= 100:
            slot = "YTD3M"
        elif fp == "Q2" and 160 <= span <= 200:
            slot = "YTD6M"
        elif fp == "Q3" and 250 <= span <= 290:
            slot = "YTD9M"
        elif fp == "FY" and 340 <= span <= 380:
            slot = "FY"
        if slot is None:
            continue
        by_fy_slot.setdefault((fy_int, slot), []).append(f)

    def _latest(fs):
        return max(
            fs,
            key=lambda x: (_as_date(_safe(x, "filing_date"))
                           if _safe(x, "filing_date") else date.min),
        )

    # Try each YTD position in the most recent FY (largest fiscal_year)
    # whose period_end <= as_of. (target is already pre-filtered to
    # filing_date <= as_of, but we additionally guard ttm_pe <= as_of
    # below as a belt-and-suspenders PIT-safety check.)
    if not by_fy_slot:
        return None
    latest_fy = max(k[0] for k in by_fy_slot.keys())

    # Walk YTD positions newest → oldest in latest_fy
    for slot in ("YTD9M", "YTD6M", "YTD3M"):
        if (latest_fy, slot) not in by_fy_slot:
            continue
        if (latest_fy - 1, "FY") not in by_fy_slot:
            continue
        if (latest_fy - 1, slot) not in by_fy_slot:
            continue

        ytd_curr = _latest(by_fy_slot[(latest_fy, slot)])
        fy_prior = _latest(by_fy_slot[(latest_fy - 1, "FY")])
        ytd_prior = _latest(by_fy_slot[(latest_fy - 1, slot)])
        try:
            v_curr = float(_safe(ytd_curr, "numeric_value"))
            v_fy = float(_safe(fy_prior, "numeric_value"))
            v_pri = float(_safe(ytd_prior, "numeric_value"))
        except (TypeError, ValueError):
            continue
        ttm_value = v_curr + (v_fy - v_pri)

        # period_end is the period_end of YTD-current.
        ttm_pe_raw = _safe(ytd_curr, "period_end")
        if ttm_pe_raw is None:
            continue
        try:
            ttm_pe = _as_date(ttm_pe_raw)
        except Exception:  # noqa: BLE001
            continue
        # PIT safety: refuse to return a TTM whose as_of_date is after
        # the caller's as_of (impossible given pre-filter, but explicit).
        if ttm_pe > as_of:
            continue
        return TTMMetric(
            concept=_safe(ytd_curr, "concept") or concept,
            label=_safe(ytd_curr, "label"),
            value=ttm_value,
            unit=_safe(ytd_curr, "unit"),
            as_of_date=ttm_pe,
            periods=[(latest_fy, slot), (latest_fy - 1, f"FY-{slot}")],
            period_facts=[ytd_curr, fy_prior, ytd_prior],
            has_gaps=False,
            has_calculated_q4=False,
            warning=(f"TTM via YTD+prior-tail fallback "
                     f"({slot}-{latest_fy} + (FY-{latest_fy-1} - "
                     f"{slot}-{latest_fy-1}))"),
        )
    return None


def _maybe_relabel_fact(fact, fy_end_month: int | None):
    """Return the fact with corrected `(fiscal_year, fiscal_period)` labels
    when the original tags don't match the period_end.

    Returns the fact (possibly replaced) when it can be classified;
    returns None when the period can't be slotted into a standard
    quarter/YTD shape (caller should drop it).

    Why re-tag instead of drop: comparative-period columns in 10-Qs are
    routinely tagged with the FILING'S `(fiscal_year, fiscal_period)`,
    not the FACT'S. Dropping them loses the underlying data that
    `TTMCalculator` legitimately needs to derive prior-year quarters
    (e.g. CBRE's comparative Q1-2019 in the 2020 Q1 10-Q is required to
    compute the TTM rolling window through Q2-2020). Re-tagging keeps the
    data with the correct labels so the calculator can use it for the
    right fiscal year rather than mis-attributing it to the current year.
    """
    if fy_end_month is None:
        return fact
    fp_orig = _safe(fact, "fiscal_period")
    fy_orig = _safe(fact, "fiscal_year")
    pe_raw = _safe(fact, "period_end")
    ps_raw = _safe(fact, "period_start")
    if pe_raw is None or ps_raw is None or fp_orig is None:
        return fact
    try:
        span = (_as_date(pe_raw) - _as_date(ps_raw)).days + 1
        fy_orig_int = int(fy_orig) if fy_orig is not None else None
    except Exception:  # noqa: BLE001
        return fact

    canonical = _canonical_fy_fp(pe_raw, span, fy_end_month)
    if canonical is None:
        # Off-cycle / mid-year transition — leave the original labels
        # alone (it might be legitimately unusual).
        return fact

    fy_canon, fp_canon = canonical
    if fy_orig_int == fy_canon and fp_orig == fp_canon:
        return fact   # already correctly labeled

    # Build a corrected copy of the fact. `dataclasses.replace` works on
    # the @dataclass(slots=True) FinancialFact.
    try:
        from dataclasses import replace
        return replace(fact, fiscal_year=fy_canon, fiscal_period=fp_canon)
    except (TypeError, ValueError):
        # If `replace` fails (immutable / non-dataclass / unknown field
        # name), drop the mis-tagged fact — better than feeding a wrong
        # label into TTMCalculator.
        return None


def _period_span_consistent(fact) -> bool:
    """Reject facts whose ``fiscal_period`` label is internally inconsistent
    with their actual period span.

    Background: 10-Qs routinely re-tag *comparative* prior-year values
    using the FILING'S fiscal_period label rather than the FACT'S period
    label. The result is fact records like
    ``fiscal_period="Q1", period_start="2022-01-01", period_end="2022-12-31"``
    — a 365-day span labelled as a single quarter — which represent a
    sub-revenue disclosure (segment / product-line breakout) rather than
    the consolidated total. ``edgar.ttm.TTMCalculator`` mistakes these
    for the consolidated FY value and produces nonsensical TTM aggregates
    (e.g. CHD 2023-Q2 TTM revenue = $311M instead of $5,375M; AXON
    2024-Q2 TTM revenue = $461M instead of $1,672M). Symptom: a
    synthesised "Q4" computed as
    ``WrongFY − YTDQ3 = $179M − $3,940M = −$3,761M`` that drags TTM down.

    The semantically-correct fact has ``fiscal_period`` matching the
    period span: ``"FY"`` for ~365 days, ``"Q1"`` for ~90 days,
    ``"Q2"`` for ~90 (single Q) or ~180 (H1 YTD), ``"Q3"`` for ~90 or
    ~270, ``"Q4"`` for ~90 or ~365.

    Returns ``True`` to KEEP the fact, ``False`` to drop it. Tolerant of
    missing fields (returns True so we don't accidentally over-prune
    facts the source library tagged loosely).
    """
    fp = _safe(fact, "fiscal_period")
    ps = _safe(fact, "period_start")
    pe = _safe(fact, "period_end")
    if fp is None or ps is None or pe is None:
        return True
    try:
        span = (_as_date(pe) - _as_date(ps)).days + 1
    except Exception:  # noqa: BLE001
        return True
    # Allow some calendar slack (53-week years, leap years, off-cycle quarters).
    if fp == "FY":
        return 340 <= span <= 380          # full year
    if fp == "Q1":
        return 70 <= span <= 100           # single Q1 only
    if fp == "Q2":
        return 70 <= span <= 100 or 160 <= span <= 200   # single Q2 OR H1 YTD
    if fp == "Q3":
        return 70 <= span <= 100 or 250 <= span <= 290   # single Q3 OR 9M YTD
    if fp == "Q4":
        return 70 <= span <= 100 or 340 <= span <= 380   # single Q4 OR FY YTD
    return True   # other labels (M3, mid-year transitions, etc.) — leave alone


# ── CIK override (delisted / inactive / share-class-format issuers) ──────
# Loaded once at first use from `cik_overrides.yaml` in the project root.
# Empty dict when the file is absent; intentional — the skill stays standalone.
@lru_cache(maxsize=1)
def _cik_overrides() -> dict[str, int]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "cik_overrides.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml  # local import: yaml isn't a hard dep of this module otherwise
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        _LOG.warning("could not load %s: %s", path, e)
        return {}
    return {str(k).upper(): int(v) for k, v in (spec.get("overrides") or {}).items()}


def _cik_override_for(ticker: str) -> int | None:
    return _cik_overrides().get(ticker.strip().upper())


def _wrap_instant(fact) -> TTMMetric:
    """Box a single instant fact in a TTMMetric for surface-compatible returns."""
    fy = _safe(fact, "fiscal_year")
    fp = _safe(fact, "fiscal_period")
    return TTMMetric(
        concept=_safe(fact, "concept"),
        label=_safe(fact, "label"),
        value=_safe(fact, "numeric_value"),
        unit=_safe(fact, "unit"),
        as_of_date=_as_date(_safe(fact, "period_end")),
        periods=[(fy, fp)] if fy is not None and fp is not None else [],
        period_facts=[fact],
        has_gaps=False,
        has_calculated_q4=False,
        warning=None,
    )


# ── Core ───────────────────────────────────────────────────────────────────
class FinancialFacts:
    """Thread-safe, lazily-loaded XBRL facts cache for one issuer.

    Cheap to construct; the EDGAR fact fetch is deferred to first ``.facts``
    access and memoised via double-checked locking on a per-instance RLock.
    The outer ``_EDGAR_LOCK`` is held only across ``Company(...)`` construction
    (which can mutate edgar's shared global session); the heavier fact-list
    materialisation runs lock-free so concurrent tickers can execute in
    parallel rather than serialising on the network round-trip.
    """
    __slots__ = ("ticker", "_facts", "_lock")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._facts: list | None = None
        self._lock = RLock()

    # -- public -------------------------------------------------------------
    @property
    def facts(self) -> list:
        """Cached list of every FinancialFact for this issuer."""
        f = self._facts
        if f is None:
            with self._lock:
                f = self._facts
                if f is None:
                    f = self._resolve()
                    self._facts = f
        return f

    # -- resolution ---------------------------------------------------------
    def _resolve(self) -> list:
        # Consult cik_overrides.yaml for delisted / inactive / share-class-
        # format-mismatched tickers (RTN, UTX, BFB, BRKB, ATVI, ALXN, ...).
        # When a ticker has an explicit CIK override, use Company(cik)
        # directly — bypasses EDGAR's current-only ticker map. The override
        # file lives in the project root and is built by cik_resolver.py;
        # if it doesn't exist, we silently fall back to ticker-based lookup
        # so the skill stays usable on its own.
        override_cik = _cik_override_for(self.ticker)
        with _EDGAR_LOCK:
            company = (Company(override_cik) if override_cik is not None
                       else Company(self.ticker))
        return list(company.facts._facts)

    # -- routing ------------------------------------------------------------
    def _period_type(self, concept: str) -> str | None:
        """Return ``'instant'`` / ``'duration'`` for the concept, or ``None``."""
        candidates = _concept_candidates(concept)
        for f in self.facts:
            if (_safe(f, "concept") in candidates
                    and _safe(f, "period_type") is not None):
                return _safe(f, "period_type")
        return None

    # -- duration (TTM) -----------------------------------------------------
    def _ttm(
        self, concept: str, as_of: date, *, split_adjust: bool,
    ) -> TTMMetric:
        candidates = _concept_candidates(concept)
        pit = [f for f in self.facts
               if _safe(f, "filing_date") is not None
               and _as_date(_safe(f, "filing_date")) <= as_of]
        if split_adjust:
            pit = apply_split_adjustments(pit, detect_splits(pit))
        # Two-stage filter:
        #   1. span-only check (catches CHD/AXON-style segment breakouts
        #      tagged with the filing's fiscal_period label).
        #   2. FY-aware RE-LABEL: rewrite (fiscal_year, fiscal_period) to
        #      match the period_end. Comparative-period columns in 10-Qs
        #      inherit the FILING'S labels (e.g. CBRE's prior-year Q1
        #      column gets fy=2020 fp=Q1 with period_end=2019-03-31).
        #      Re-tagging fixes the labels so TTMCalculator can use the
        #      data correctly as Q1 of the PRIOR year, instead of mis-
        #      counting it as Q1 of the current year (which would double-
        #      count Q1 in the TTM and inflate it).
        fy_end_month = _infer_fy_end_month(pit, candidates)
        target = []
        for f in pit:
            if _safe(f, "concept") not in candidates:
                continue
            if not _period_span_consistent(f):
                continue
            relabeled = _maybe_relabel_fact(f, fy_end_month)
            if relabeled is None:
                continue
            target.append(relabeled)
        if not target:
            raise ValueError(
                f"No facts for {concept} available as of {as_of}"
            )
        metric = TTMCalculator(target).calculate_ttm(as_of=as_of)

        # Fallback chain. Each fallback is a strict algebraic identity —
        # no heuristic interpolation, no look-ahead. We try them in order
        # of expected yield × safety. Each operates on the same pre-
        # filtered, re-labeled `target` set so the PIT guarantee carries
        # through every path.
        #
        #   #1 — Q4 = FY − (Q1 + Q2 + Q3)
        #        Augments the input set with a synthesized Q4 fact when
        #        FY + three single quarters of the same fy are available.
        #        Re-runs TTMCalculator, which now has a complete same-
        #        fiscal-year quarter ladder to pick from.
        #
        #   #2 — FY-anchor: when as_of falls within ±30 days of a
        #        reported FY-end, return that FY value directly. The TTM
        #        ending at FY-end IS the FY value by definition.
        #
        #   #3 — YTD-current + (FY-prior − YTD-prior-same-position):
        #        e.g. TTM(mid-Q3-2025) = YTD6M-2025 + (FY-2024 − YTD6M-2024)
        #        Useful when single-quarter facts aren't reported but
        #        cumulative YTDs are (banks, insurers, some utilities).
        if getattr(metric, "has_gaps", False):
            augmented = _augment_with_synthesized_q4s(target, fy_end_month)
            if len(augmented) > len(target):
                metric = TTMCalculator(augmented).calculate_ttm(as_of=as_of)
        if getattr(metric, "has_gaps", False):
            anchor = _try_fy_anchor(target, as_of, concept)
            if anchor is not None:
                metric = anchor
        if getattr(metric, "has_gaps", False):
            ytd_chain = _try_ytd_plus_prior_tail(
                target, as_of, fy_end_month, concept,
            )
            if ytd_chain is not None:
                metric = ytd_chain

        # Post-condition: keep every TTM whose four constituent quarters'
        # period_ends are ~90 days apart (consecutive) — even when the
        # quarters were INTERPOLATED. TTMCalculator's interpolation paths
        # (Q4=FY−YTD9M, Q3=YTD9M−YTD6M, Q2=YTD6M−Q1) all produce a
        # synthesized quarter with a correct period_end when the YTD
        # operand is from the SAME fiscal year. In that case the resulting
        # window is genuinely 12 months long and has_gaps=False (e.g.
        # CNP 2021-11-28: Q4-2020 derived as FY-2020 − YTD9M-2020 →
        # consecutive ends 2020-12-31, 2021-03-31, 2021-06-30, 2021-09-30
        # → kept, $5M post-Texas-freeze TTM is correct).
        #
        # We only reject when has_gaps=True, which happens when:
        #   * `_select_ttm_window` fell back to non-consecutive quarters
        #     (PWR OCF 2019-08-27 stitched Q2-2018, Q3-2018, Q1-2019,
        #     Q2-2019 — 182-day gap from Q3-2018 to Q1-2019 because no
        #     Q4-2018 was derivable, value collapsed to $4M).
        #   * Interpolation produced a synthetic quarter with a multi-
        #     year span by pairing FY with a STALE-YEAR YTD (CBRE
        #     CostOfRevenue 2020-06-01: synthesized "Q4-2018" with span
        #     2016-10-01..2018-12-31 because FY-2018 was paired with
        #     YTD9M-2016, doubling TTM to $43B).
        # Both produce period_ends that are NOT consecutive — that's the
        # signal the assembled value isn't a real 12-month aggregate.
        if getattr(metric, "has_gaps", False):
            ends_str = ""
            if metric.period_facts:
                ends = sorted(
                    str(_as_date(_safe(f, "period_end")))
                    for f in metric.period_facts
                    if _safe(f, "period_end") is not None
                )
                ends_str = f" period_ends={ends}"
            raise ValueError(
                f"TTM rejected: TTMCalculator's 4-quarter window has "
                f"non-consecutive period_ends (has_gaps=True), so the "
                f"sum doesn't represent a real 12-month aggregate. "
                f"Reported + same-year-interpolated quarters with "
                f"consecutive ends are still kept; this rejection only "
                f"fires when the assembly itself spans a calendar gap. "
                f"concept={concept}, as_of={as_of}{ends_str}"
            )
        return metric

    # -- instant (balance-sheet snapshot) ----------------------------------
    def _instant(
        self, concept: str, as_of: date, *, split_adjust: bool,
    ) -> TTMMetric:
        candidates = _concept_candidates(concept)
        pit_all = [f for f in self.facts
                   if _safe(f, "filing_date") is not None
                   and _as_date(_safe(f, "filing_date")) <= as_of]
        target = [
            f for f in pit_all
            if _safe(f, "concept") in candidates
            and _safe(f, "period_type") == "instant"
            and _safe(f, "period_end") is not None
            and _as_date(_safe(f, "period_end")) <= as_of
        ]
        if not target:
            raise ValueError(
                f"No instant facts for {concept} available as of {as_of}"
            )
        if split_adjust:
            target = apply_split_adjustments(target, detect_splits(pit_all))
        # Latest period_end wins; tie-break on latest filing_date so an
        # amendment (10-Q/A, 10-K/A) supersedes the original for the same
        # period_end.
        chosen = max(
            target,
            key=lambda f: (
                _as_date(_safe(f, "period_end")),
                _as_date(_safe(f, "filing_date")),
            ),
        )
        return _wrap_instant(chosen)

    # -- single value -------------------------------------------------------
    @_timed
    def pit_value(
        self,
        concept: str,
        as_of: str | date,
        *,
        split_adjust: bool = True,
    ) -> TTMMetric:
        """Auto-routed PIT value: TTM for duration, snapshot for instant."""
        as_of_date = _as_date(as_of)
        ptype = self._period_type(concept)
        if ptype is None:
            raise ValueError(f"No facts found for concept: {concept}")
        if ptype == "instant":
            return self._instant(concept, as_of_date, split_adjust=split_adjust)
        return self._ttm(concept, as_of_date, split_adjust=split_adjust)

    # -- time series --------------------------------------------------------
    @_timed
    def pit_series(
        self,
        concept: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        split_adjust: bool = True,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Build a PIT time series + per-observation source-fact audit frame.

        Observation dates are the filing dates at which this concept appears.
        Each observation re-runs ``pit_value`` as of that date so the value
        reflects only what was knowable then.
        """
        candidates = _concept_candidates(concept)
        concept_facts = [
            f for f in self.facts
            if _safe(f, "concept") in candidates
            and _safe(f, "filing_date") is not None
        ]
        if not concept_facts:
            raise ValueError(f"No facts found for concept: {concept}")

        start_d = _as_date(start) if start is not None else None
        end_d = _as_date(end) if end is not None else None
        obs_dates = sorted({_as_date(_safe(f, "filing_date"))
                            for f in concept_facts})
        if start_d is not None:
            obs_dates = [d for d in obs_dates if d >= start_d]
        if end_d is not None:
            obs_dates = [d for d in obs_dates if d <= end_d]

        ts_rows: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []
        for obs in obs_dates:
            try:
                m = self.pit_value(concept, obs, split_adjust=split_adjust)
            except Exception as e:  # noqa: BLE001
                ts_rows.append({
                    "observation_date": obs, "period_end": None,
                    "concept": concept, "label": None, "value": None,
                    "unit": None, "periods": None, "has_gaps": None,
                    "has_calculated_q4": None, "warning": str(e),
                })
                continue
            ts_rows.append({
                "observation_date": obs, "period_end": m.as_of_date,
                "concept": m.concept, "label": m.label, "value": m.value,
                "unit": m.unit, "periods": m.periods, "has_gaps": m.has_gaps,
                "has_calculated_q4": m.has_calculated_q4, "warning": m.warning,
            })
            for i, f in enumerate(m.period_facts, start=1):
                audit_rows.append({
                    "observation_date": obs, "period_end": m.as_of_date,
                    "value": m.value, "source_number": i,
                    "source_concept": _safe(f, "concept"),
                    "source_label": _safe(f, "label"),
                    "source_value": _safe(f, "numeric_value"),
                    "source_unit": _safe(f, "unit"),
                    "source_period_start": _safe(f, "period_start"),
                    "source_period_end": _safe(f, "period_end"),
                    "source_fiscal_year": _safe(f, "fiscal_year"),
                    "source_fiscal_period": _safe(f, "fiscal_period"),
                    "source_filing_date": _safe(f, "filing_date"),
                    "source_form_type": _safe(f, "form_type"),
                    "source_accession": _safe(f, "accession"),
                    "calculation_context": _safe(f, "calculation_context"),
                })

        ts = (pd.DataFrame(ts_rows, columns=list(_TS_COLS))
              .sort_values("observation_date", kind="mergesort")
              .reset_index(drop=True))
        audit = (pd.DataFrame(audit_rows, columns=list(_AUDIT_COLS))
                 .sort_values(["observation_date", "source_number"],
                              kind="mergesort")
                 .reset_index(drop=True))
        return ts, audit


# ── Functional façade ──────────────────────────────────────────────────────
@lru_cache(maxsize=2048)
def _fetcher(ticker: str) -> FinancialFacts:
    return FinancialFacts(ticker)


@_validate_ticker
@_validate_concept
def get_pit_value(
    ticker: str,
    concept: str,
    as_of: str | date,
    *,
    split_adjust: bool = True,
) -> TTMMetric:
    """Return the PIT value for ``concept`` on ``ticker`` as of ``as_of``.

    Parameters
    ----------
    ticker : str
        Issuer ticker, e.g. "AAPL".
    concept : str
        XBRL concept (e.g. "NetIncomeLoss", "Assets"). Bare names are tried
        as raw, ``us-gaap:``, and ``ifrs-full:`` prefixes.
    as_of : str | date
        Cutoff date. Only facts with ``filing_date <= as_of`` are considered.
    split_adjust : bool
        Apply detected stock-split adjustments before aggregation. Default True.

    Returns
    -------
    edgar.ttm.TTMMetric
        TTM aggregate (4 source facts in ``period_facts``) for duration
        concepts; latest snapshot (1 source fact) for instant concepts.
    """
    return _fetcher(ticker).pit_value(concept, as_of, split_adjust=split_adjust)


@_validate_ticker
@_validate_concept
def get_pit_series(
    ticker: str,
    concept: str,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    split_adjust: bool = True,
    with_audit: bool = False,
):
    """Return a PIT time series for ``concept`` on ``ticker``.

    Parameters
    ----------
    ticker, concept, split_adjust :
        See ``get_pit_value``.
    start, end : date-like, optional
        Inclusive observation-date window. ``None`` means no bound.
    with_audit : bool
        When True, return ``(ts_df, audit_df)``. When False (default), return
        only ``ts_df`` so the API matches steps 1-3.

    Returns
    -------
    pandas.DataFrame, or (pandas.DataFrame, pandas.DataFrame)
        Time-series frame with one row per observation date; audit frame with
        the source facts behind each observation (included when
        ``with_audit=True``).
    """
    ts, audit = _fetcher(ticker).pit_series(
        concept, start=start, end=end, split_adjust=split_adjust,
    )
    return (ts, audit) if with_audit else ts


# ── Batch (multi-ticker) façade ────────────────────────────────────────────
def _clean_tickers(tickers: Iterable[str]) -> list[str]:
    """Dedup + strip + drop empty / non-string entries; sorted for determinism."""
    return sorted({t.strip() for t in tickers
                   if isinstance(t, str) and t.strip()})


_VALUE_BATCH_COLS: tuple[str, ...] = (
    "ticker", "concept", "label", "as_of_date", "value", "unit",
    "periods", "has_gaps", "has_calculated_q4", "warning",
)


@_validate_concept
def get_pit_value_batch(
    tickers: Iterable[str],
    concept: str,
    as_of: str | date,
    *,
    split_adjust: bool = True,
    max_workers: int = 8,
) -> pd.DataFrame:
    """Parallel ``get_pit_value`` over many tickers; one row per ticker.

    Parameters
    ----------
    tickers : iterable of str
        Issuer tickers. Deduplicated and whitespace-stripped; empty / non-string
        entries dropped.
    concept, as_of, split_adjust :
        See ``get_pit_value``.
    max_workers : int
        Parallel ticker lookups (default 8). Lower to 1-2 if you hit HTTP 429.

    Returns
    -------
    pandas.DataFrame
        Columns ``ticker``, ``concept``, ``label``, ``as_of_date``, ``value``,
        ``unit``, ``periods``, ``has_gaps``, ``has_calculated_q4``, ``warning``.
        Per-ticker failures are logged at DEBUG and skipped — the batch never
        raises on a single bad ticker.
    """
    unique = _clean_tickers(tickers)
    if not unique:
        return pd.DataFrame(columns=list(_VALUE_BATCH_COLS))
    workers = min(max_workers, len(unique)) or 1
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="pit-value",
    ) as pool:
        futs = {
            pool.submit(get_pit_value, t, concept, as_of,
                        split_adjust=split_adjust): t
            for t in unique
        }
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                m = fut.result()
            except Exception as e:  # noqa: BLE001
                _LOG.debug("get_pit_value(%r, %r) failed: %s", t, concept, e)
                continue
            rows.append({
                "ticker": t, "concept": m.concept, "label": m.label,
                "as_of_date": m.as_of_date, "value": m.value, "unit": m.unit,
                "periods": m.periods, "has_gaps": m.has_gaps,
                "has_calculated_q4": m.has_calculated_q4, "warning": m.warning,
            })
    return (pd.DataFrame(rows, columns=list(_VALUE_BATCH_COLS))
            .sort_values("ticker", kind="mergesort")
            .reset_index(drop=True))


@_validate_concept
def get_pit_series_batch(
    tickers: Iterable[str],
    concept: str,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    split_adjust: bool = True,
    max_workers: int = 8,
    with_audit: bool = False,
):
    """Parallel ``get_pit_series`` over many tickers; long frame with ``ticker``.

    Parameters
    ----------
    tickers : iterable of str
        Issuer tickers. Deduplicated and whitespace-stripped.
    concept, start, end, split_adjust :
        See ``get_pit_series``.
    max_workers : int
        Parallel ticker lookups (default 8).
    with_audit : bool
        When True, returns ``(ts_long, audit_long)`` — both with a ``ticker``
        column prepended. When False (default), returns just ``ts_long``.

    Returns
    -------
    pandas.DataFrame, or (pandas.DataFrame, pandas.DataFrame)
        Long-format time-series (one row per ticker × observation date), and
        optionally the corresponding source-fact audit frame.
    """
    unique = _clean_tickers(tickers)
    ts_cols = ("ticker", *_TS_COLS)
    audit_cols = ("ticker", *_AUDIT_COLS)
    if not unique:
        empty_ts = pd.DataFrame(columns=list(ts_cols))
        empty_audit = pd.DataFrame(columns=list(audit_cols))
        return (empty_ts, empty_audit) if with_audit else empty_ts
    workers = min(max_workers, len(unique)) or 1
    ts_frames: list[pd.DataFrame] = []
    audit_frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="pit-series",
    ) as pool:
        futs = {
            pool.submit(_fetcher(t).pit_series, concept,
                        start=start, end=end, split_adjust=split_adjust): t
            for t in unique
        }
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                ts, audit = fut.result()
            except Exception as e:  # noqa: BLE001
                _LOG.debug("get_pit_series(%r, %r) failed: %s", t, concept, e)
                continue
            if not ts.empty:
                ts_frames.append(ts.assign(ticker=t))
            if with_audit and not audit.empty:
                audit_frames.append(audit.assign(ticker=t))
    ts_long = (pd.concat(ts_frames, ignore_index=True, copy=False)[list(ts_cols)]
               if ts_frames
               else pd.DataFrame(columns=list(ts_cols)))
    if ts_frames:
        ts_long = (ts_long
                   .sort_values(["ticker", "observation_date"], kind="mergesort")
                   .reset_index(drop=True))
    if not with_audit:
        return ts_long
    audit_long = (pd.concat(audit_frames, ignore_index=True, copy=False)
                  [list(audit_cols)]
                  if audit_frames
                  else pd.DataFrame(columns=list(audit_cols)))
    if audit_frames:
        audit_long = (audit_long
                      .sort_values(["ticker", "observation_date", "source_number"],
                                   kind="mergesort")
                      .reset_index(drop=True))
    return ts_long, audit_long


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
        description=("Fetch point-in-time financial values from SEC EDGAR "
                     "(auto-routes TTM vs balance-sheet snapshot).")
    )
    p.add_argument("tickers", nargs="+",
                   help="Issuer ticker(s), e.g. AAPL MSFT NVDA. Multi-ticker "
                        "runs use parallel fetches and produce a long-format frame.")
    p.add_argument("concept", help="XBRL concept, e.g. NetIncomeLoss, Assets.")
    p.add_argument("--as-of", default=None,
                   help="Single PIT lookup at this date (YYYY-MM-DD). "
                        "Mutually exclusive with --start/--end.")
    p.add_argument("--start", default=None,
                   help="Series start date YYYY-MM-DD (inclusive).")
    p.add_argument("--end", default=None,
                   help="Series end date YYYY-MM-DD (inclusive).")
    p.add_argument("--no-split-adjust", dest="split_adjust",
                   action="store_false",
                   help="Skip stock-split adjustment.")
    p.add_argument("--workers", type=int, default=8,
                   help="Max parallel ticker lookups for multi-ticker runs.")
    p.add_argument("--identity", default=None,
                   help="SEC contact email. Falls back to $EDGAR_IDENTITY.")
    p.add_argument("--out", default=None,
                   help="Write the time-series frame to this path "
                        "(.parquet or .csv).")
    p.add_argument("--audit-out", default=None,
                   help="Also write the audit frame to this path "
                        "(.parquet or .csv); only valid in series mode.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Emit DEBUG-level timing logs.")
    args = p.parse_args(argv)

    if args.as_of and (args.start or args.end):
        p.error("--as-of is mutually exclusive with --start / --end.")
    if args.as_of and args.audit_out:
        p.error("--audit-out is only meaningful in series mode.")

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    ident = args.identity or os.environ.get("EDGAR_IDENTITY")
    if not ident:
        p.error("Identity required: pass --identity or set EDGAR_IDENTITY.")
    set_identity(ident)

    single = len(args.tickers) == 1

    if args.as_of:
        if single:
            m = get_pit_value(args.tickers[0], args.concept, args.as_of,
                              split_adjust=args.split_adjust)
            print(f"{args.tickers[0]} {m.concept} as of {m.as_of_date}: "
                  f"{m.value} {m.unit}")
            if m.warning:
                print(f"warning: {m.warning}")
            return 0
        df = get_pit_value_batch(
            args.tickers, args.concept, args.as_of,
            split_adjust=args.split_adjust, max_workers=args.workers,
        )
        if args.out:
            _write(df, args.out, p)
        else:
            print(df.to_string(index=False))
            print(f"\n{len(df):,} rows x {df.shape[1]} columns")
        return 0

    if single:
        ts, audit = _fetcher(args.tickers[0].strip()).pit_series(
            args.concept.strip(), start=args.start, end=args.end,
            split_adjust=args.split_adjust,
        )
    else:
        ts, audit = get_pit_series_batch(
            args.tickers, args.concept,
            start=args.start, end=args.end,
            split_adjust=args.split_adjust, max_workers=args.workers,
            with_audit=True,
        )
    if args.out:
        _write(ts, args.out, p)
    else:
        print(ts.head().to_string())
        print(f"\n{len(ts):,} rows x {ts.shape[1]} columns")
    if args.audit_out:
        _write(audit, args.audit_out, p)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
