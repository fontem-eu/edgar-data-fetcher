#!/usr/bin/env python3
"""
Fixture Capture Script
=======================
Downloads real SEC data and saves it as fixture files for the test suite.

Run this script once to (re)generate the fixtures from live SEC APIs.
The output files are committed to the repo — tests run offline against them.

What gets downloaded
--------------------
- reference/company_tickers.json       ~3 MB  (full SEC ticker list)
- reference/company_tickers_mf.json    ~2 MB  (mutual fund tickers)
- reference/company_tickers_exchange.json ~5 MB (tickers by exchange)
- reference/ticker.txt                 ~1 MB  (CIK:ticker mapping)
- companyfacts/CIK0000320193.json      ~5 MB  (Apple Inc.)
- companyfacts/CIK0000789019.json      ~5 MB  (Microsoft Corp)

Total: ~20 MB — suitable for committing directly to the repository.

Usage
-----
    # From within the edgar-gmr-etl virtualenv:
    python tests/fixtures/capture.py

    # Override the tickers whose facts you want to capture:
    python tests/fixtures/capture.py --tickers AAPL MSFT NVDA TSLA
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

# pylint: disable=import-outside-toplevel  # httpx/urllib are optional/stdlib

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

FIXTURES_DIR     = Path(__file__).parent
REFERENCE_DIR    = FIXTURES_DIR / "reference"
COMPANYFACTS_DIR = FIXTURES_DIR / "companyfacts"

SEC_BASE_URL = "https://www.sec.gov"
SEC_DATA_URL = "https://data.sec.gov"
USER_AGENT   = "edgar-gmr-etl/1.0 bemar-edgar@research.com"

# Known CIKs for the default tickers (zero-padded, 10 digits)
DEFAULT_TICKERS = {
    "AAPL": 320193,
    "MSFT": 789019,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, description: str = "") -> bytes:
    """Download *url* and return the raw bytes, preferring httpx over urllib."""
    label = description or url.split("/")[-1]
    print(f"  Downloading {label}…", end=" ", flush=True)
    t0 = time.perf_counter()

    try:
        import httpx
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=60,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.content
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()

    print(f"{len(data) / 1024:.0f} KB in {time.perf_counter() - t0:.1f}s")
    return data


def _save(path: Path, data: bytes) -> None:
    """Write raw bytes to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _save_json_pretty(path: Path, data: bytes) -> None:
    """Save JSON with indentation for readability and smaller diffs."""
    parsed = json.loads(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parsed, indent=2))


def _parse_ticker_map(raw: bytes) -> dict[str, int]:
    """Parse a company_tickers.json payload into a {TICKER: cik} mapping."""
    data = json.loads(raw)
    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
    return {
        v["ticker"].upper(): v["cik_str"]
        for v in data.values()
        if "ticker" in v
    }


# ---------------------------------------------------------------------------
# Download stages
# ---------------------------------------------------------------------------

def capture_reference() -> None:
    """Download the four SEC reference data files."""
    print("\n-- Reference data --")

    files = [
        (f"{SEC_BASE_URL}/files/company_tickers.json",           "company_tickers.json"),
        (f"{SEC_BASE_URL}/files/company_tickers_mf.json",        "company_tickers_mf.json"),
        (f"{SEC_BASE_URL}/files/company_tickers_exchange.json",  "company_tickers_exchange.json"),
        (f"{SEC_BASE_URL}/include/ticker.txt",                   "ticker.txt"),
    ]

    for url, filename in files:
        dest = REFERENCE_DIR / filename
        if dest.exists():
            print(f"  Skipping {filename} (already exists)")
            continue
        try:
            data = _get(url, filename)
            if filename.endswith(".json"):
                _save_json_pretty(dest, data)
            else:
                _save(dest, data)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"  WARN: failed to download {filename}: {exc}")


def capture_company_facts(tickers: dict[str, int]) -> None:
    """
    Download per-company facts JSON files from the SEC's XBRL API.

    These are the individual CIK*.json files that would normally be
    unpacked from companyfacts.zip when running setup.py --mode facts.
    """
    print("\n-- Company facts --")

    for ticker, cik in tickers.items():
        filename = f"CIK{cik:010d}.json"
        dest     = COMPANYFACTS_DIR / filename
        if dest.exists():
            print(f"  Skipping {filename} ({ticker}, already exists)")
            continue
        url = f"{SEC_DATA_URL}/api/xbrl/companyfacts/CIK{cik:010d}.json"
        try:
            data = _get(url, f"{filename} ({ticker})")
            _save_json_pretty(dest, data)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"  WARN: failed to download {filename}: {exc}")


def resolve_ciks(tickers: list[str]) -> dict[str, int]:
    """
    Map ticker symbols to CIK numbers using the local fixture or a live lookup.

    Tries the local company_tickers.json fixture first (avoids a network call
    when the reference fixture already exists). Falls back to the SEC API.
    """
    local = REFERENCE_DIR / "company_tickers.json"
    if local.exists():
        by_ticker = _parse_ticker_map(local.read_bytes())
    else:
        raw = _get(
            f"{SEC_BASE_URL}/files/company_tickers.json",
            "company_tickers.json (for CIK lookup)",
        )
        by_ticker = _parse_ticker_map(raw)

    mapping: dict[str, int] = {}
    for t in tickers:
        t_upper = t.upper()
        if t_upper in by_ticker:
            mapping[t_upper] = by_ticker[t_upper]
        else:
            print(f"  WARN: ticker {t_upper!r} not found in SEC ticker list — skipping")

    return mapping


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="capture.py",
        description="Download real SEC data to use as test fixtures.",
    )
    p.add_argument(
        "--tickers", "-t",
        nargs="+",
        default=list(DEFAULT_TICKERS.keys()),
        metavar="TICKER",
        help=(
            "Ticker symbols whose company facts to capture. "
            f"Default: {' '.join(DEFAULT_TICKERS.keys())}"
        ),
    )
    p.add_argument(
        "--skip-reference",
        action="store_true",
        help="Skip downloading reference data (company_tickers.json etc.).",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files that already exist locally.",
    )
    return p


def main() -> None:
    """Entry point: parse args and run the fixture capture."""
    parser = _build_parser()
    args   = parser.parse_args()

    if args.overwrite:
        # Remove existing files so the skip-checks don't short-circuit
        for path in list(REFERENCE_DIR.glob("*")) + list(COMPANYFACTS_DIR.glob("*.json")):
            if path.is_file():
                path.unlink()
                print(f"Removed {path.name}")

    print(f"Saving fixtures to: {FIXTURES_DIR}")

    if not args.skip_reference:
        capture_reference()

    tickers_to_ciks = resolve_ciks(args.tickers)
    capture_company_facts(tickers_to_ciks)

    print("\nDone. Fixture files:")
    for d in (REFERENCE_DIR, COMPANYFACTS_DIR):
        for f in sorted(d.glob("*")):
            kb = f.stat().st_size / 1024
            print(f"  {f.relative_to(FIXTURES_DIR)}  ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
