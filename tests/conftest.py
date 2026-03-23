"""
Shared pytest fixtures for edgar-data-init tests.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

# pylint: disable=redefined-outer-name    # pytest fixture injection pattern
# pylint: disable=import-outside-toplevel # edgar imports are deferred by design
# pylint: disable=broad-exception-caught  # intentional: cache may not exist
# pylint: disable=unused-argument         # fixtures used for side effects

# ---------------------------------------------------------------------------
# Make setup.py importable from any working directory
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent          # edgar-data-init/
FIXTURES = Path(__file__).parent / "fixtures"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Also add edgar-gmr-etl venv to sys.path so edgartools is importable
VENV_SITE = ROOT.parent / "edgar-gmr-etl" / ".venv" / "lib"
if VENV_SITE.exists():
    for p in sorted(VENV_SITE.glob("python*/site-packages")):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
            break


# ---------------------------------------------------------------------------
# Directory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """An empty temporary data directory passed to setup.py functions."""
    d = tmp_path / "edgar-data"
    d.mkdir()
    return d


@pytest.fixture()
def data_dir_with_facts(data_dir: Path) -> Path:
    """
    A data directory pre-populated with the synthetic company facts fixtures.
    Enables EntityFacts to resolve AAPL and MSFT fully offline.
    """
    facts_dir = data_dir / "companyfacts"
    facts_dir.mkdir()
    for src in (FIXTURES / "companyfacts").glob("CIK*.json"):
        shutil.copy(src, facts_dir / src.name)
    return data_dir


# ---------------------------------------------------------------------------
# Local-storage environment helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_storage_env(data_dir_with_facts: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Configure the edgartools local-storage environment variables to point
    at data_dir_with_facts.  Also clears the in-process EntityFacts cache
    so each test starts clean.

    Returns the data directory path.
    """
    monkeypatch.setenv("EDGAR_USE_LOCAL_DATA", "1")
    monkeypatch.setenv("EDGAR_LOCAL_DATA_DIR", str(data_dir_with_facts))

    # Clear module-level cache so previous test results don't leak
    try:
        from edgar.entity.entity_facts import _company_facts_cache
        _company_facts_cache.clear()
    except Exception:
        pass

    yield data_dir_with_facts

    # Restore: unset vars (monkeypatch handles this automatically, but
    # explicitly clearing the edgartools cache prevents cross-test leakage)
    try:
        from edgar.entity.entity_facts import _company_facts_cache
        _company_facts_cache.clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fixture data accessors
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def aapl_facts_json() -> dict:
    """The synthetic AAPL company facts JSON as a dict."""
    return json.loads((FIXTURES / "companyfacts" / "CIK0000320193.json").read_text())


@pytest.fixture(scope="session")
def msft_facts_json() -> dict:
    """The synthetic MSFT company facts JSON as a dict."""
    return json.loads((FIXTURES / "companyfacts" / "CIK0000789019.json").read_text())
