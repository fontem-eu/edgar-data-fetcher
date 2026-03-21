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

    # Force re-download even if already complete:
    python setup.py --mode full --force

    # Set how often to refresh (days, 0 = never re-download):
    python setup.py --mode full --ttl-days 30

    # Control log verbosity (1=error 2=warn 3=info 4=debug):
    python setup.py --mode full --verbosity 4

State tracking
--------------
A .state.yaml file is written to the data directory to track download
progress.  If the script is interrupted, the next run resumes from the
last incomplete stage.  Stages marked 'complete' are skipped unless the
data is older than --ttl-days or --force is passed.

Environment written
-------------------
After a successful run, an edgar.env file is written next to this script.
Source it (or add to your shell profile / .env) before running main.py:

    source /config/edgar-data-init/edgar.env

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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# rich is a hard dependency (in the project venv alongside edgartools).
# edgartools and pyyaml imports are deferred to the functions that need them
# so the module stays importable for tests that mock those call sites.
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
DEFAULT_TTL_DAYS  = 7
DEFAULT_VERBOSITY = 3
ENV_FILE          = Path(__file__).parent / "edgar.env"
STATE_FILE_NAME   = ".state.yaml"

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
log.addHandler(logging.NullHandler())  # silence if caller never calls _configure_logging


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
# State management
# ---------------------------------------------------------------------------

def _load_state(data_dir: Path) -> dict[str, Any]:
    """Load .state.yaml from data_dir; returns empty dict if absent or unreadable."""
    state_file = data_dir / STATE_FILE_NAME
    if not state_file.exists():
        log.debug("No state file at %s", state_file)
        return {}
    try:
        import yaml
        state = yaml.safe_load(state_file.read_text()) or {}
        log.debug("Loaded state: %s", {k: v.get("status") for k, v in state.items()})
        return state
    except Exception:  # pylint: disable=broad-exception-caught
        log.debug("Could not parse state file %s — treating as empty", state_file)
        return {}


def _save_state(data_dir: Path, state: dict[str, Any]) -> None:
    """Persist state dict to .state.yaml."""
    import yaml
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / STATE_FILE_NAME
    state_file.write_text(yaml.dump(state, default_flow_style=False))
    log.debug("State saved (%d stage(s))", len(state))


def _now_iso() -> str:
    """Return the current UTC time as a compact ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_in_progress(data_dir: Path, stage: str) -> None:
    state = _load_state(data_dir)
    state.setdefault(stage, {})["status"] = "in_progress"
    state[stage]["started_at"] = _now_iso()
    _save_state(data_dir, state)
    log.debug("Stage %r → in_progress", stage)


def _mark_complete(data_dir: Path, stage: str, file_count: int = 0) -> None:
    state = _load_state(data_dir)
    state.setdefault(stage, {})["status"] = "complete"
    state[stage]["completed_at"] = _now_iso()
    state[stage]["file_count"] = file_count
    _save_state(data_dir, state)
    log.debug("Stage %r → complete (%d files)", stage, file_count)


def _stage_decision(  # pylint: disable=too-many-return-statements
    data_dir: Path, stage: str, ttl_days: int, force: bool
) -> tuple[bool, str]:
    """
    Return (needs_work, human_readable_reason) for a stage.

    Callers use `needs_work` to decide whether to download and `reason`
    for the log message that tells the operator what will happen and why.
    The 8 return paths map directly to the 8 distinct state transitions —
    collapsing them would obscure the decision tree.
    """
    if force:
        return True, "force flag set"

    state  = _load_state(data_dir)
    info   = state.get(stage, {})
    status = info.get("status")

    if status == "in_progress":
        started = info.get("started_at", "unknown time")
        return True, f"interrupted (started {started}) — resuming"

    if status != "complete":
        return True, "not yet downloaded"

    # Stage is complete — check TTL
    completed_at = info.get("completed_at")
    count        = info.get("file_count", 0)

    if ttl_days == 0:
        date_str = completed_at[:10] if completed_at else "unknown"
        return False, f"complete ({date_str}, {count:,} files, TTL disabled)"

    if not completed_at:
        return True, "complete but timestamp missing — re-downloading"

    try:
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        age_days  = (datetime.now(timezone.utc) - completed).days
        date_str  = completed_at[:10]
        if age_days >= ttl_days:
            return True, f"stale ({date_str}, age={age_days}d ≥ TTL={ttl_days}d) — re-downloading"
        return False, f"complete ({date_str}, {count:,} files, age={age_days}d)"
    except Exception:  # pylint: disable=broad-exception-caught
        return True, "complete but unparseable timestamp — re-downloading"


def _stage_needs_work(data_dir: Path, stage: str, ttl_days: int, force: bool) -> bool:
    """Return True if the stage should (re-)run. Thin wrapper around _stage_decision."""
    needs_work, _ = _stage_decision(data_dir, stage, ttl_days, force)
    return needs_work


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _size_str(path: Path) -> str:
    """Human-readable size of a directory or file."""
    if not path.exists():
        return "0 B"
    if path.is_file():
        b = path.stat().st_size
    else:
        b = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


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
# Download stages
# ---------------------------------------------------------------------------

def download_reference(
    data_dir: Path, ttl_days: int = DEFAULT_TTL_DAYS, force: bool = False
) -> None:
    """Download ticker/CIK reference mappings (~50 MB)."""
    needs_work, reason = _stage_decision(data_dir, "reference", ttl_days, force)

    if not needs_work:
        log.info("[cyan]reference[/cyan]   %s — skipping", reason)
        return

    log.info("[cyan]reference[/cyan]   %s", reason)
    log.info("Downloading reference data (ticker/CIK mappings, ~50 MB)...")
    _mark_in_progress(data_dir, "reference")
    t0 = time.perf_counter()

    from edgar.storage._local import download_reference_data
    download_reference_data()

    ref_dir = data_dir / "reference"
    count   = sum(1 for f in ref_dir.glob("*") if f.is_file()) if ref_dir.exists() else 0
    _mark_complete(data_dir, "reference", file_count=count)
    log.info(
        "[green]reference complete[/green] — %d files, %s, %.0fs",
        count, _size_str(ref_dir), time.perf_counter() - t0,
    )


def download_facts(
    data_dir: Path, ttl_days: int = DEFAULT_TTL_DAYS, force: bool = False
) -> None:
    """
    Download pre-processed XBRL company facts for every public company (~1.5-2 GB
    compressed, unpacks to one CIK*.json per company under companyfacts/).

    This replaces the need for XBRLS.from_filings() entirely: EntityFacts reads
    the local JSON files directly, making financial data lookups instant.
    """
    needs_work, reason = _stage_decision(data_dir, "companyfacts", ttl_days, force)

    if not needs_work:
        log.info(
            "[cyan]companyfacts[/cyan] %s — skipping (%s)",
            reason, _size_str(data_dir / "companyfacts"),
        )
        return

    log.info("[cyan]companyfacts[/cyan] %s", reason)
    log.info(
        "Downloading company facts (~1.5-2 GB download, expect 5-30 min)..."
    )
    _mark_in_progress(data_dir, "companyfacts")
    t0 = time.perf_counter()

    from edgar.storage._local import download_facts as _dl_facts
    _dl_facts(disable_progress=False)

    facts_dir = data_dir / "companyfacts"
    count     = sum(1 for _ in facts_dir.glob("CIK*.json")) if facts_dir.exists() else 0
    _mark_complete(data_dir, "companyfacts", file_count=count)
    log.info(
        "[green]companyfacts complete[/green] — %s companies, %s, %.0fs",
        f"{count:,}", _size_str(facts_dir), time.perf_counter() - t0,
    )


def download_submissions(
    data_dir: Path, ttl_days: int = DEFAULT_TTL_DAYS, force: bool = False
) -> None:
    """
    Download company submission indexes (~1.5 GB compressed).

    Provides company metadata (name, SIC, exchange) and a full index of every
    filing ever made. Enables company search and filing discovery offline.
    """
    needs_work, reason = _stage_decision(data_dir, "submissions", ttl_days, force)

    if not needs_work:
        log.info(
            "[cyan]submissions[/cyan]  %s — skipping (%s)",
            reason, _size_str(data_dir / "submissions"),
        )
        return

    log.info("[cyan]submissions[/cyan]  %s", reason)
    log.info("Downloading submissions index (~1.5 GB, expect 5-20 min)...")
    _mark_in_progress(data_dir, "submissions")
    t0 = time.perf_counter()

    from edgar.storage._local import download_submissions as _dl_subs
    _dl_subs(disable_progress=False)

    sub_dir = data_dir / "submissions"
    count   = sum(1 for _ in sub_dir.glob("CIK*.json")) if sub_dir.exists() else 0
    _mark_complete(data_dir, "submissions", file_count=count)
    log.info(
        "[green]submissions complete[/green] — %s companies, %s, %.0fs",
        f"{count:,}", _size_str(sub_dir), time.perf_counter() - t0,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(data_dir: Path, test_tickers: list[str] | None = None) -> bool:
    """
    Smoke-test the local setup by looking up a few tickers via EntityFacts.
    Returns True if everything works.
    """
    from edgar import Company

    tickers = test_tickers or ["AAPL", "MSFT"]
    log.info(
        "Verifying EntityFacts lookup for %s (data-dir: %s)",
        ", ".join(tickers), data_dir,
    )
    all_ok = True

    for ticker in tickers:
        log.debug("Testing %s...", ticker)
        try:
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
    state = _load_state(data_dir)

    table = Table(
        title="EDGAR Local Data",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold",
    )
    table.add_column("Layer",        style="cyan",  min_width=14)
    table.add_column("Status",       min_width=10)
    table.add_column("Size",         justify="right")
    table.add_column("Files",        justify="right")
    table.add_column("Last updated", justify="right", style="dim")

    layers = {
        "reference":    data_dir / "reference",
        "companyfacts": data_dir / "companyfacts",
        "submissions":  data_dir / "submissions",
        "filings":      data_dir / "filings",
    }

    for name, path in layers.items():
        info       = state.get(name, {})
        updated    = (info.get("completed_at") or "")[:10]
        file_count = info.get("file_count")

        if path.exists() and any(path.iterdir()):
            if file_count is None:
                file_count = sum(1 for _ in path.rglob("*.json"))
            table.add_row(
                name,
                "[green]present[/green]",
                _size_str(path),
                f"{file_count:,}",
                updated or "—",
            )
        else:
            table.add_row(name, "[dim]missing[/dim]", "—", "—", "—")

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
        "--ttl-days",
        type=int,
        default=DEFAULT_TTL_DAYS,
        metavar="N",
        help=(
            "Re-download a completed stage if its data is older than N days. "
            "0 means never re-download (default: %(default)s)."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Force re-download of all stages regardless of state.",
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
    ttl_days: int  = args.ttl_days
    force: bool    = args.force

    log.info(
        "EDGAR setup — mode=[bold]%s[/bold]  data-dir=%s  TTL=%s  force=%s",
        mode,
        data_dir,
        f"{ttl_days}d" if ttl_days else "disabled",
        force,
    )

    # ── Create data directory ─────────────────────────────────────────────
    data_dir.mkdir(parents=True, exist_ok=True)
    log.debug("Data directory ready: %s", data_dir)

    # ── Bootstrap edgartools ──────────────────────────────────────────────
    try:
        _setup_edgartools(data_dir, identity)
    except ImportError as exc:
        log.error(
            "Could not import edgartools: %s\n"
            "  Make sure you are running inside the project virtualenv:\n"
            "    source .venv/bin/activate && python setup.py",
            exc,
        )
        sys.exit(1)

    # ── Download stages ───────────────────────────────────────────────────
    log.info("--- download stages ---")

    download_reference(data_dir, ttl_days=ttl_days, force=force)

    if mode in ("facts", "full"):
        download_facts(data_dir, ttl_days=ttl_days, force=force)

    if mode == "full":
        download_submissions(data_dir, ttl_days=ttl_days, force=force)

    # ── Write .env ────────────────────────────────────────────────────────
    write_env(data_dir, identity)

    # ── Verify ───────────────────────────────────────────────────────────
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

    # ── Summary ───────────────────────────────────────────────────────────
    print_summary(data_dir, mode)


if __name__ == "__main__":
    main()
