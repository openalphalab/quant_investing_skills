"""Sync vendored copies of shared modules between skills.

Each skill folder under .claude/skills/ must be independently installable, so
shared code is vendored (copied) rather than imported across skills. This
script keeps those copies honest.

Vendor map below declares (canonical, [vendored_copies]) pairs. The canonical
file is the single source of truth; vendored copies receive a banner header
identifying the canonical and are otherwise byte-equal to it.

Usage:
    python tools/sync_vendored.py            # write all vendored copies
    python tools/sync_vendored.py --check    # exit 1 if any copy drifts (CI)

Add to pre-commit / CI to prevent accidental drift.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / ".claude" / "skills"

VENDOR_MAP: list[tuple[Path, list[Path]]] = [
    (
        SKILLS / "fetching-fund-universe" / "scripts" / "fund_holdings.py",
        [SKILLS / "classifying-industry" / "scripts" / "fund_holdings.py"],
    ),
]

BANNER_BEGIN = "# >>> VENDORED — DO NOT EDIT >>>"
BANNER_END = "# <<< VENDORED <<<"


def _banner(canonical: Path) -> str:
    rel = canonical.relative_to(REPO).as_posix()
    return (
        f"{BANNER_BEGIN}\n"
        f"# Canonical source: {rel}\n"
        f"# Refresh this copy with: python tools/sync_vendored.py\n"
        f"# Verify in CI with:     python tools/sync_vendored.py --check\n"
        f"{BANNER_END}\n"
    )


def _strip_banner(text: str) -> str:
    """Remove a vendored banner block if present (idempotent)."""
    if not text.startswith(BANNER_BEGIN):
        return text
    end_marker = f"{BANNER_END}\n"
    idx = text.find(end_marker)
    return text[idx + len(end_marker):] if idx != -1 else text


def _expected(canonical: Path) -> str:
    return _banner(canonical) + _strip_banner(canonical.read_text(encoding="utf-8"))


def _check(verbose: bool = True) -> int:
    drift = 0
    for canonical, copies in VENDOR_MAP:
        if not canonical.exists():
            print(f"ERROR: canonical missing: {canonical}", file=sys.stderr)
            drift += 1
            continue
        want = _expected(canonical)
        for dst in copies:
            if not dst.exists():
                print(f"DRIFT  (missing): {dst}")
                drift += 1
                continue
            got = dst.read_text(encoding="utf-8")
            if got != want:
                print(f"DRIFT  {dst}")
                drift += 1
            elif verbose:
                print(f"OK     {dst}")
    return 1 if drift else 0


def _write() -> int:
    written = 0
    for canonical, copies in VENDOR_MAP:
        if not canonical.exists():
            print(f"ERROR: canonical missing: {canonical}", file=sys.stderr)
            return 2
        want = _expected(canonical)
        for dst in copies:
            dst.parent.mkdir(parents=True, exist_ok=True)
            existing = dst.read_text(encoding="utf-8") if dst.exists() else None
            if existing == want:
                print(f"unchanged  {dst}")
                continue
            dst.write_text(want, encoding="utf-8")
            print(f"wrote      {dst}")
            written += 1
    print(f"\n{written} file(s) updated")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--check",
        action="store_true",
        help="Verify vendored copies match canonical; exit non-zero on drift.",
    )
    args = p.parse_args(argv)
    return _check() if args.check else _write()


if __name__ == "__main__":
    sys.exit(main())
