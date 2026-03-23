#!/usr/bin/env python3
"""
EDGAR Local Data Setup Script
==============================
Downloads and prepares SEC EDGAR bulk data for the edgar-gmr-etl investment
engine to run fully offline.

Modes
-----
smoke   Download only reference data (~50 MB).
        Validates auth, directories, and edgartools config. Fast.

facts   Download reference + company facts (~3.5 GB compressed).
        Gives you a complete local copy of pre-processed XBRL financial data
        for every public company. Enables EntityFacts to replace the slow
        XBRLS.from_filings() pipeline.

full    Download reference + facts + submissions (~5 GB compressed).
        Adds company metadata and filing index search on top of facts.

Usage
-----
    python setup.py --mode smoke
    python setup.py --mode facts --data-dir /data/edgar
    python setup.py --mode full  --identity you@example.com

    # Control log verbosity (1=error 2=warn 3=info 4=debug):
    python setup.py --mode full --verbosity 4

Scheduling
----------
Run this script on a schedule to keep data fresh.  The recommended approach
is a weekly Kubernetes CronJob (see deployment/templates/cronjob.yaml).
Each run downloads the full dataset for the chosen mode from scratch.

Environment written
-------------------
After a successful run, an edgar.env file is written next to this script.
Source it (or add to your shell profile / .env) before running the engine:

    source /config/edgar-data-fetcher/edgar.env

What is NOT downloaded
----------------------
Raw SGML filing bundles (the daily Feed/*.nc.tar.gz archives) are NOT
downloaded here. Those are only needed for raw filing text / attachments.
For financial data (balance sheet, income statement, cash flow) the
pre-processed company facts files are sufficient and much smaller.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# rich is a hard dependency (in the project venv alongside edgartools).
# edgartools imports are deferred to the functions that need them so the
# module stays importable for tests that mock those call sites.
# pylint: disable=import-outside-toplevel

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich import box

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR  = Path("/config/edgar-data")
DEFAULT_IDENTITY  = "bemar-edgar@research.com"
DEFAULT_VERBOSITY = 3
ENV_FILE          = Path(__file__).parent / "edgar.env"
METADATA_FILE_NAME = "download.yaml"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

VERBOSITY_TO_LEVEL = {
    1: logging.ERROR,
    2: logging.WARNING,
    3: logging.INFO,
    4: logging.DEBUG,
}

log = logging.getLogger("edgar_setup")
log.addHandler(logging.NullHandler())  # silence when _configure_logging is not called (tests)


def _configure_logging(verbosity: int) -> None:
    """Wire up a RichHandler at the level chosen by --verbosity."""
    level = VERBOSITY_TO_LEVEL.get(verbosity, logging.INFO)
    handler = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_path=False,
        log_time_format="[%X]",
    )
    handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    log.handlers = [handler]
    log.setLevel(level)
    log.propagate = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _size_str(path: Path) -> str:
    """Human-readable size of a directory or file."""
    if not path.exists():
        return "0 B"
    b: float = (
        path.stat().st_size if path.is_file()
        else sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    )
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _write_metadata(directory: Path, **fields) -> None:
    """Write download metadata as YAML to *directory*/download.yaml."""
    import yaml
    (directory / METADATA_FILE_NAME).write_text(
        yaml.dump(fields, default_flow_style=False, sort_keys=False)
    )


def _setup_edgartools(data_dir: Path, identity: str) -> None:
    """Configure edgartools to use local storage at *data_dir*."""
    import edgar
    from edgar.storage import use_local_storage
    edgar.set_identity(identity)
    use_local_storage(data_dir)
    log.debug(
        "edgartools configured — EDGAR_LOCAL_DATA_DIR=%s, EDGAR_USE_LOCAL_DATA=1",
        data_dir,
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def run_download(data_dir: Path, mode: str) -> None:
    """Download EDGAR bulk data for *mode* using edgar.storage.download_edgar_data."""
    size_hints = {"smoke": "~50 MB", "facts": "~3.5 GB", "full": "~5 GB"}
    log.info("Downloading EDGAR data into %s — mode=%s (%s)...", data_dir, mode, size_hints[mode])
    if mode in ("facts", "full"):
        log.info("Large download — expect 10-30 min.")

    t0 = time.perf_counter()

    from edgar.storage import download_edgar_data
    download_edgar_data(
        reference=True,
        facts=(mode in ("facts", "full")),
        submissions=(mode == "full"),
        disable_progress=False,
    )

    log.info("Download finished in %.0fs", time.perf_counter() - t0)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(data_dir: Path, test_tickers: list[str] | None = None) -> bool:
    """
    Smoke-test the local setup by looking up a few tickers via EntityFacts.
    Returns True if everything works.
    """
    tickers = test_tickers or ["AAPL", "MSFT"]
    log.info("Verifying EntityFacts at %s for: %s", data_dir, ", ".join(tickers))
    all_ok = True

    for ticker in tickers:
        log.debug("Testing %s...", ticker)
        try:
            from edgar import Company
            company = Company(ticker)
            facts   = company.get_facts()
            if facts is None:
                raise RuntimeError("get_facts() returned None")
            log.info("  [green]OK[/green]  %s — %s", ticker, type(facts).__name__)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("  [red]FAIL[/red] %s — %s", ticker, exc)
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------

def write_env(data_dir: Path, identity: str) -> None:
    """Write an edgar.env file that projects can source."""
    env_content = f"""\
# EDGAR local storage configuration
# Generated by setup.py — source this before running the investment engine:
#   source {ENV_FILE}

export EDGAR_LOCAL_DATA_DIR="{data_dir}"
export EDGAR_USE_LOCAL_DATA="1"
export EDGAR_IDENTITY="{identity}"
"""
    ENV_FILE.write_text(env_content)
    log.info("Wrote [bold]%s[/bold]", ENV_FILE)
    log.info("Source it with:  source %s", ENV_FILE)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(data_dir: Path, mode: str) -> None:
    """Render a rich table summarising what data is present on disk."""
    table = Table(
        title="EDGAR Local Data",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
    )
    table.add_column("Layer",  style="cyan", min_width=14)
    table.add_column("Status", min_width=10)
    table.add_column("Size",   justify="right")
    table.add_column("Files",  justify="right")

    layers = {
        "reference":    data_dir / "reference",
        "companyfacts": data_dir / "companyfacts",
        "submissions":  data_dir / "submissions",
        "filings":      data_dir / "filings",
    }

    for name, path in layers.items():
        if path.exists() and any(path.iterdir()):
            count = sum(1 for _ in path.rglob("*.json"))
            table.add_row(name, "[green]present[/green]", _size_str(path), f"{count:,}")
        else:
            table.add_row(name, "[dim]missing[/dim]", "—", "—")

    console = Console()
    console.print()
    console.print(table)
    console.print(f"  Total: [bold]{_size_str(data_dir)}[/bold]")
    console.print()

    hints = {
        "smoke": "Smoke test passed. Run [bold]--mode facts[/bold] to get financial data.",
        "facts": (
            "Company facts ready — EntityFacts now reads from disk.\n"
            "  Next: update edgar_fetcher.py to use EntityFacts instead of "
            "XBRLS.from_filings()."
        ),
        "full": "Full setup complete. Reference, facts, and submissions are local.",
    }
    if mode in hints:
        console.print(f"  {hints[mode]}")
        console.print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="setup.py",
        description="Download and prepare SEC EDGAR bulk data for local use.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--mode", "-m",
        choices=["smoke", "facts", "full"],
        default="smoke",
        help=(
            "smoke: reference only (~50 MB, fast test); "
            "facts: reference + company facts (~3.5 GB); "
            "full: reference + facts + submissions (~5 GB). "
            "Default: smoke"
        ),
    )
    p.add_argument(
        "--data-dir", "-d",
        type=Path,
        default=DEFAULT_DATA_DIR,
        metavar="PATH",
        help=f"Root directory for EDGAR data (default: {DEFAULT_DATA_DIR})",
    )
    p.add_argument(
        "--identity", "-i",
        default=DEFAULT_IDENTITY,
        metavar="EMAIL",
        help=(
            "E-mail sent to the SEC EDGAR API as identity. "
            f"(default: {DEFAULT_IDENTITY})"
        ),
    )
    p.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the EntityFacts verification step at the end.",
    )
    p.add_argument(
        "--verify-tickers",
        nargs="+",
        default=["AAPL", "MSFT"],
        metavar="TICKER",
        help="Tickers to test during verification (default: AAPL MSFT).",
    )
    p.add_argument(
        "--verbosity", "-v",
        type=int,
        choices=[1, 2, 3, 4],
        default=DEFAULT_VERBOSITY,
        metavar="N",
        help="Log verbosity: 1=error 2=warn 3=info 4=debug (default: %(default)s).",
    )
    return p


def main() -> None:
    """Entry point: parse args and run the requested download mode."""
    parser = _build_parser()
    args   = parser.parse_args()

    _configure_logging(args.verbosity)

    data_dir: Path = args.data_dir.expanduser().resolve()
    identity: str  = args.identity
    mode: str      = args.mode

    log.info("EDGAR setup — mode=[bold]%s[/bold]  data-dir=%s", mode, data_dir)

    tmp_dir = data_dir.parent / (data_dir.name + ".tmp")

    if tmp_dir.exists():
        log.warning("Stale tmp directory found, removing: %s", tmp_dir)
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    log.debug("Tmp directory ready: %s", tmp_dir)

    start_time = datetime.now(timezone.utc)
    _write_metadata(tmp_dir, status="in_progress", start_time=start_time.isoformat())

    try:
        _setup_edgartools(tmp_dir, identity)
    except ImportError as exc:
        log.error(
            "Could not import edgartools: %s\n"
            "  Make sure you are running inside the project virtualenv:\n"
            "    source .venv/bin/activate && python setup.py",
            exc,
        )
        shutil.rmtree(tmp_dir)
        sys.exit(1)

    try:
        run_download(tmp_dir, mode)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log.error("Download failed: %s", exc)
        shutil.rmtree(tmp_dir)
        sys.exit(1)

    end_time = datetime.now(timezone.utc)
    elapsed = (end_time - start_time).total_seconds()
    _write_metadata(
        tmp_dir,
        status="success",
        start_time=start_time.isoformat(),
        end_time=end_time.isoformat(),
        elapsed_seconds=round(elapsed, 1),
    )

    old_dir = data_dir.parent / (data_dir.name + ".old")
    if old_dir.exists():
        log.debug("Removing previous old data directory: %s", old_dir)
        shutil.rmtree(old_dir)
    if data_dir.exists():
        log.info("Archiving previous data: %s → %s", data_dir, old_dir)
        data_dir.rename(old_dir)
    log.info("Activating new data: %s → %s", tmp_dir, data_dir)
    tmp_dir.rename(data_dir)

    _setup_edgartools(data_dir, identity)

    write_env(data_dir, identity)

    if not args.skip_verify:
        facts_dir = data_dir / "companyfacts"
        if facts_dir.exists() and any(facts_dir.iterdir()):
            if not verify(data_dir, args.verify_tickers):
                log.warning(
                    "Verification failed for one or more tickers — "
                    "data may still be usable, check the errors above."
                )
        else:
            log.info(
                "Skipping EntityFacts verification "
                "(facts not downloaded in mode=[bold]%s[/bold]).",
                mode,
            )

    print_summary(data_dir, mode)


if __name__ == "__main__":
    main()
