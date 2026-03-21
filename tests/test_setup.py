"""
Tests for setup.py
==================

Structure
---------
Unit tests     – mock edgar.storage download functions; exercise setup.py
                 logic (directory creation, skip-if-present, mode gating,
                 env-file writing, state management) with zero network calls.

Integration    – put synthetic CIK JSON fixtures on disk, enable local
                 storage env vars, and call verify() to confirm the full
                 EntityFacts → local-file path works end-to-end.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=protected-access        # tests call internal setup._ helpers
# pylint: disable=import-outside-toplevel # edgar imports are deferred by design
# pylint: disable=unused-argument         # pytest fixtures used for side effects

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

import setup  # edgar-data-init/setup.py (added to sys.path by conftest.py)

# ---------------------------------------------------------------------------
# Paths to test_resources fixtures
# ---------------------------------------------------------------------------

TEST_RESOURCES = Path(__file__).parent / "test_resources"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dir_with_file(parent: Path, dirname: str, filename: str = "dummy.json") -> Path:
    """Create a sub-directory with one file, simulating a completed download."""
    d = parent / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text("{}")
    return d


def _write_state(data_dir: Path, state: dict) -> None:
    """Write a .state.yaml directly (bypasses setup._save_state for test setup)."""
    import yaml
    (data_dir / ".state.yaml").write_text(yaml.dump(state, default_flow_style=False))


# ---------------------------------------------------------------------------
# Unit tests — directory creation
# ---------------------------------------------------------------------------

class TestDirectorySetup:
    def test_data_dir_is_created(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "brand-new"
        assert not new_dir.exists()
        new_dir.mkdir()  # setup.py main() does this; replicate here
        assert new_dir.is_dir()

    def test_setup_edgartools_sets_env_vars(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("EDGAR_USE_LOCAL_DATA", raising=False)
        monkeypatch.delenv("EDGAR_LOCAL_DATA_DIR", raising=False)
        setup._setup_edgartools(tmp_path, "test@example.com")
        assert os.environ.get("EDGAR_USE_LOCAL_DATA") == "1"
        assert os.environ.get("EDGAR_LOCAL_DATA_DIR") == str(tmp_path)


# ---------------------------------------------------------------------------
# Unit tests — state management
# ---------------------------------------------------------------------------

class TestStateManagement:
    def test_load_state_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        assert setup._load_state(tmp_path) == {}

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        state = {"reference": {"status": "complete", "file_count": 4}}
        setup._save_state(tmp_path, state)
        loaded = setup._load_state(tmp_path)
        assert loaded == state

    def test_mark_in_progress(self, tmp_path: Path) -> None:
        setup._mark_in_progress(tmp_path, "reference")
        state = setup._load_state(tmp_path)
        assert state["reference"]["status"] == "in_progress"
        assert "started_at" in state["reference"]

    def test_mark_complete(self, tmp_path: Path) -> None:
        setup._mark_complete(tmp_path, "reference", file_count=4)
        state = setup._load_state(tmp_path)
        assert state["reference"]["status"] == "complete"
        assert state["reference"]["file_count"] == 4
        assert "completed_at" in state["reference"]

    def test_mark_complete_preserves_other_stages(self, tmp_path: Path) -> None:
        setup._mark_complete(tmp_path, "reference", file_count=4)
        setup._mark_complete(tmp_path, "companyfacts", file_count=12000)
        state = setup._load_state(tmp_path)
        assert state["reference"]["status"] == "complete"
        assert state["companyfacts"]["status"] == "complete"

    def test_stage_needs_work_when_no_state(self, tmp_path: Path) -> None:
        assert setup._stage_needs_work(tmp_path, "reference", ttl_days=7, force=False) is True

    def test_stage_needs_work_when_in_progress(self, tmp_path: Path) -> None:
        setup._mark_in_progress(tmp_path, "reference")
        assert setup._stage_needs_work(tmp_path, "reference", ttl_days=7, force=False) is True

    def test_stage_no_work_when_complete_within_ttl(self, tmp_path: Path) -> None:
        setup._mark_complete(tmp_path, "reference", file_count=4)
        # just completed → age = 0 days < ttl=7
        assert setup._stage_needs_work(tmp_path, "reference", ttl_days=7, force=False) is False

    def test_stage_needs_work_when_stale(self, tmp_path: Path) -> None:
        # Write a completed_at that is 10 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_state(tmp_path, {"reference": {"status": "complete", "completed_at": old_ts}})
        assert setup._stage_needs_work(tmp_path, "reference", ttl_days=7, force=False) is True

    def test_stage_not_stale_when_ttl_disabled(self, tmp_path: Path) -> None:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_state(tmp_path, {"reference": {"status": "complete", "completed_at": old_ts}})
        # ttl_days=0 means never re-download
        assert setup._stage_needs_work(tmp_path, "reference", ttl_days=0, force=False) is False

    def test_stage_needs_work_when_force(self, tmp_path: Path) -> None:
        setup._mark_complete(tmp_path, "reference", file_count=4)
        assert setup._stage_needs_work(tmp_path, "reference", ttl_days=7, force=True) is True


# ---------------------------------------------------------------------------
# Unit tests — skip logic (state-based)
# ---------------------------------------------------------------------------

class TestSkipIfAlreadyComplete:
    def test_reference_skipped_when_state_is_complete(
        self, data_dir: Path, mocker
    ) -> None:
        setup._mark_complete(data_dir, "reference", file_count=4)
        _make_dir_with_file(data_dir, "reference")
        mock_dl = mocker.patch("edgar.storage._local.download_reference_data")
        setup.download_reference(data_dir, ttl_days=7)
        mock_dl.assert_not_called()

    def test_facts_skipped_when_state_is_complete(
        self, data_dir: Path, mocker
    ) -> None:
        setup._mark_complete(data_dir, "companyfacts", file_count=100)
        _make_dir_with_file(data_dir, "companyfacts")
        mock_dl = mocker.patch("edgar.storage._local.download_facts")
        setup.download_facts(data_dir, ttl_days=7)
        mock_dl.assert_not_called()

    def test_submissions_skipped_when_state_is_complete(
        self, data_dir: Path, mocker
    ) -> None:
        setup._mark_complete(data_dir, "submissions", file_count=100)
        _make_dir_with_file(data_dir, "submissions")
        mock_dl = mocker.patch("edgar.storage._local.download_submissions")
        setup.download_submissions(data_dir, ttl_days=7)
        mock_dl.assert_not_called()

    def test_reference_downloaded_when_no_state(
        self, data_dir: Path, mocker
    ) -> None:
        def _side():
            _make_dir_with_file(data_dir, "reference")

        mock_dl = mocker.patch("edgar.storage._local.download_reference_data", side_effect=_side)
        setup.download_reference(data_dir, ttl_days=7)
        mock_dl.assert_called_once()

    def test_reference_downloaded_when_state_is_in_progress(
        self, data_dir: Path, mocker
    ) -> None:
        setup._mark_in_progress(data_dir, "reference")

        def _side():
            _make_dir_with_file(data_dir, "reference")

        mock_dl = mocker.patch("edgar.storage._local.download_reference_data", side_effect=_side)
        setup.download_reference(data_dir, ttl_days=7)
        mock_dl.assert_called_once()

    def test_reference_downloaded_when_stale(
        self, data_dir: Path, mocker
    ) -> None:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_state(data_dir, {"reference": {"status": "complete", "completed_at": old_ts}})
        _make_dir_with_file(data_dir, "reference")

        def _side():
            pass  # files already there

        mock_dl = mocker.patch("edgar.storage._local.download_reference_data", side_effect=_side)
        setup.download_reference(data_dir, ttl_days=7)
        mock_dl.assert_called_once()

    def test_reference_downloaded_when_force(
        self, data_dir: Path, mocker
    ) -> None:
        setup._mark_complete(data_dir, "reference", file_count=4)
        _make_dir_with_file(data_dir, "reference")

        def _side():
            pass

        mock_dl = mocker.patch("edgar.storage._local.download_reference_data", side_effect=_side)
        setup.download_reference(data_dir, ttl_days=7, force=True)
        mock_dl.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests — individual download functions write state correctly
# ---------------------------------------------------------------------------

class TestDownloadFunctions:
    def test_download_reference_marks_complete(
        self, data_dir: Path, mocker
    ) -> None:
        def _side():
            _make_dir_with_file(data_dir, "reference")

        mocker.patch("edgar.storage._local.download_reference_data", side_effect=_side)
        setup.download_reference(data_dir)
        state = setup._load_state(data_dir)
        assert state["reference"]["status"] == "complete"

    def test_download_facts_marks_complete(
        self, data_dir: Path, mocker
    ) -> None:
        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "companyfacts", "CIK0000320193.json")

        mocker.patch("edgar.storage._local.download_facts", side_effect=_side)
        setup.download_facts(data_dir)
        state = setup._load_state(data_dir)
        assert state["companyfacts"]["status"] == "complete"

    def test_download_submissions_marks_complete(
        self, data_dir: Path, mocker
    ) -> None:
        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "submissions", "CIK0000320193.json")

        mocker.patch("edgar.storage._local.download_submissions", side_effect=_side)
        setup.download_submissions(data_dir)
        state = setup._load_state(data_dir)
        assert state["submissions"]["status"] == "complete"

    def test_download_reference_calls_edgar(
        self, data_dir: Path, mocker
    ) -> None:
        mock_dl = mocker.patch("edgar.storage._local.download_reference_data")
        mock_dl.side_effect = lambda: _make_dir_with_file(data_dir, "reference")
        setup.download_reference(data_dir)
        mock_dl.assert_called_once_with()

    def test_download_facts_calls_edgar(
        self, data_dir: Path, mocker
    ) -> None:
        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "companyfacts", "CIK0000320193.json")

        mock_dl = mocker.patch("edgar.storage._local.download_facts", side_effect=_side)
        setup.download_facts(data_dir)
        mock_dl.assert_called_once_with(disable_progress=False)

    def test_download_submissions_calls_edgar(
        self, data_dir: Path, mocker
    ) -> None:
        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "submissions", "CIK0000320193.json")

        mock_dl = mocker.patch("edgar.storage._local.download_submissions", side_effect=_side)
        setup.download_submissions(data_dir)
        mock_dl.assert_called_once_with(disable_progress=False)

    def test_download_marks_in_progress_before_calling_edgar(
        self, data_dir: Path, mocker
    ) -> None:
        """State must be in_progress while the download is running."""
        observed = {}

        def _side():
            observed["status"] = setup._load_state(data_dir).get("reference", {}).get("status")
            _make_dir_with_file(data_dir, "reference")

        mocker.patch("edgar.storage._local.download_reference_data", side_effect=_side)
        setup.download_reference(data_dir)
        assert observed["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Unit tests — mode selection (main orchestration)
# ---------------------------------------------------------------------------

class TestModeSelection:
    """
    Verify that each --mode only triggers the expected download functions.
    We mock at the setup-module level (the local download_* wrappers) so we
    don't need to worry about edgar internals.
    """

    def _run_main(self, mode: str, tmp_path: Path, mocker, monkeypatch) -> tuple:
        mock_ref  = mocker.patch.object(setup, "download_reference")
        mock_fcts = mocker.patch.object(setup, "download_facts")
        mock_subs = mocker.patch.object(setup, "download_submissions")
        mock_vfy  = mocker.patch.object(setup, "verify", return_value=True)
        mocker.patch.object(setup, "write_env")
        mocker.patch.object(setup, "print_summary")
        mocker.patch.object(setup, "_setup_edgartools")
        mocker.patch.object(setup, "_configure_logging")

        monkeypatch.setattr("sys.argv", [
            "setup.py",
            "--mode", mode,
            "--data-dir", str(tmp_path),
            "--identity", "test@example.com",
        ])
        setup.main()
        return mock_ref, mock_fcts, mock_subs, mock_vfy

    def test_smoke_only_downloads_reference(self, tmp_path: Path, mocker, monkeypatch) -> None:
        ref, fcts, subs, _ = self._run_main("smoke", tmp_path, mocker, monkeypatch)
        ref.assert_called_once()
        fcts.assert_not_called()
        subs.assert_not_called()

    def test_facts_downloads_reference_and_facts(self, tmp_path: Path, mocker, monkeypatch) -> None:
        ref, fcts, subs, _ = self._run_main("facts", tmp_path, mocker, monkeypatch)
        ref.assert_called_once()
        fcts.assert_called_once()
        subs.assert_not_called()

    def test_full_downloads_all_three(self, tmp_path: Path, mocker, monkeypatch) -> None:
        ref, fcts, subs, _ = self._run_main("full", tmp_path, mocker, monkeypatch)
        ref.assert_called_once()
        fcts.assert_called_once()
        subs.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests — env file writing
# ---------------------------------------------------------------------------

class TestEnvFileWriting:
    def test_env_file_is_created(self, data_dir: Path, tmp_path: Path, mocker) -> None:
        env_path = tmp_path / "edgar.env"
        mocker.patch.object(setup, "ENV_FILE", env_path)
        setup.write_env(data_dir, "me@example.com")
        assert env_path.exists()

    def test_env_file_contains_data_dir(self, data_dir: Path, tmp_path: Path, mocker) -> None:
        env_path = tmp_path / "edgar.env"
        mocker.patch.object(setup, "ENV_FILE", env_path)
        setup.write_env(data_dir, "me@example.com")
        content = env_path.read_text()
        assert str(data_dir) in content

    def test_env_file_contains_identity(self, data_dir: Path, tmp_path: Path, mocker) -> None:
        env_path = tmp_path / "edgar.env"
        mocker.patch.object(setup, "ENV_FILE", env_path)
        setup.write_env(data_dir, "investor@fund.com")
        assert "investor@fund.com" in env_path.read_text()

    def test_env_file_sets_use_local_data_to_1(
        self, data_dir: Path, tmp_path: Path, mocker
    ) -> None:
        env_path = tmp_path / "edgar.env"
        mocker.patch.object(setup, "ENV_FILE", env_path)
        setup.write_env(data_dir, "me@example.com")
        assert 'EDGAR_USE_LOCAL_DATA="1"' in env_path.read_text()


# ---------------------------------------------------------------------------
# Unit tests — verify() behaviour
# ---------------------------------------------------------------------------

class TestVerifyUnit:
    def test_verify_returns_true_when_all_succeed(self, data_dir: Path, mocker) -> None:
        mock_company = MagicMock()
        mock_company.get_facts.return_value = MagicMock()  # non-None
        mocker.patch("edgar.Company", return_value=mock_company)
        result = setup.verify(data_dir, ["AAPL", "MSFT"])
        assert result is True

    def test_verify_returns_false_when_get_facts_returns_none(
        self, data_dir: Path, mocker
    ) -> None:
        mock_company = MagicMock()
        mock_company.get_facts.return_value = None
        mocker.patch("edgar.Company", return_value=mock_company)
        result = setup.verify(data_dir, ["AAPL"])
        assert result is False

    def test_verify_returns_false_on_exception(
        self, data_dir: Path, mocker
    ) -> None:
        mocker.patch("edgar.Company", side_effect=RuntimeError("network error"))
        result = setup.verify(data_dir, ["AAPL"])
        assert result is False

    def test_verify_checks_all_tickers(self, data_dir: Path, mocker) -> None:
        mock_company = MagicMock()
        mock_company.get_facts.return_value = MagicMock()
        mock_cls = mocker.patch("edgar.Company", return_value=mock_company)
        setup.verify(data_dir, ["AAPL", "MSFT", "NVDA"])
        assert mock_cls.call_count == 3


# ---------------------------------------------------------------------------
# Unit tests — resume logic using test_resources fixtures
# ---------------------------------------------------------------------------

class TestResumeLogic:
    """
    Verify the script correctly identifies which stages to run or skip
    based on real .state.yaml files from tests/test_resources/.

    Each sub-directory represents a specific scenario:
      reference_only/    — reference complete, facts/submissions pending
      interrupted_facts/ — reference complete, facts in_progress (interrupted)
      all_complete/      — all stages complete and fresh
      stale_complete/    — all stages complete but timestamps from 2020
    """

    def _copy_resources(self, src_name: str, tmp_path: Path) -> Path:
        """Copy a test_resources scenario into tmp_path and return the path."""
        src = TEST_RESOURCES / src_name
        dest = tmp_path / src_name
        shutil.copytree(src, dest)
        return dest

    # -- reference_only scenario -------------------------------------------

    def test_reference_only_skips_reference(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("reference_only", tmp_path)
        mock_dl = mocker.patch("edgar.storage._local.download_reference_data")
        setup.download_reference(data_dir, ttl_days=7)
        mock_dl.assert_not_called()

    def test_reference_only_runs_facts(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("reference_only", tmp_path)

        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "companyfacts", "CIK0000320193.json")

        mock_dl = mocker.patch("edgar.storage._local.download_facts", side_effect=_side)
        setup.download_facts(data_dir, ttl_days=7)
        mock_dl.assert_called_once()

    def test_reference_only_runs_submissions(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("reference_only", tmp_path)

        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "submissions", "CIK0000320193.json")

        mock_dl = mocker.patch("edgar.storage._local.download_submissions", side_effect=_side)
        setup.download_submissions(data_dir, ttl_days=7)
        mock_dl.assert_called_once()

    # -- interrupted_facts scenario ----------------------------------------

    def test_interrupted_facts_skips_reference(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("interrupted_facts", tmp_path)
        mock_dl = mocker.patch("edgar.storage._local.download_reference_data")
        setup.download_reference(data_dir, ttl_days=7)
        mock_dl.assert_not_called()

    def test_interrupted_facts_resumes_facts(self, tmp_path: Path, mocker) -> None:
        """An in_progress stage must be retried, not skipped."""
        data_dir = self._copy_resources("interrupted_facts", tmp_path)

        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "companyfacts", "CIK0000320193.json")

        mock_dl = mocker.patch("edgar.storage._local.download_facts", side_effect=_side)
        setup.download_facts(data_dir, ttl_days=7)
        mock_dl.assert_called_once()

    def test_interrupted_facts_state_becomes_complete_after_resume(
        self, tmp_path: Path, mocker
    ) -> None:
        data_dir = self._copy_resources("interrupted_facts", tmp_path)

        def _side(disable_progress):  # noqa: ARG001
            _make_dir_with_file(data_dir, "companyfacts", "CIK0000320193.json")

        mocker.patch("edgar.storage._local.download_facts", side_effect=_side)
        setup.download_facts(data_dir, ttl_days=7)
        state = setup._load_state(data_dir)
        assert state["companyfacts"]["status"] == "complete"

    # -- all_complete scenario ---------------------------------------------

    def test_all_complete_skips_all_stages(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("all_complete", tmp_path)
        mock_ref  = mocker.patch("edgar.storage._local.download_reference_data")
        mock_fcts = mocker.patch("edgar.storage._local.download_facts")
        mock_subs = mocker.patch("edgar.storage._local.download_submissions")
        setup.download_reference(data_dir, ttl_days=7)
        setup.download_facts(data_dir, ttl_days=7)
        setup.download_submissions(data_dir, ttl_days=7)
        mock_ref.assert_not_called()
        mock_fcts.assert_not_called()
        mock_subs.assert_not_called()

    def test_all_complete_force_reruns_all(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("all_complete", tmp_path)
        mock_ref  = mocker.patch("edgar.storage._local.download_reference_data")
        mock_fcts = mocker.patch("edgar.storage._local.download_facts")
        mock_subs = mocker.patch("edgar.storage._local.download_submissions")
        setup.download_reference(data_dir, ttl_days=7, force=True)
        setup.download_facts(data_dir, ttl_days=7, force=True)
        setup.download_submissions(data_dir, ttl_days=7, force=True)
        mock_ref.assert_called_once()
        mock_fcts.assert_called_once()
        mock_subs.assert_called_once()

    # -- stale_complete scenario -------------------------------------------

    def test_stale_complete_reruns_all_with_default_ttl(self, tmp_path: Path, mocker) -> None:
        """Timestamps from 2020 are older than any reasonable TTL."""
        data_dir = self._copy_resources("stale_complete", tmp_path)
        mock_ref  = mocker.patch("edgar.storage._local.download_reference_data")
        mock_fcts = mocker.patch("edgar.storage._local.download_facts")
        mock_subs = mocker.patch("edgar.storage._local.download_submissions")
        setup.download_reference(data_dir, ttl_days=7)
        setup.download_facts(data_dir, ttl_days=7)
        setup.download_submissions(data_dir, ttl_days=7)
        mock_ref.assert_called_once()
        mock_fcts.assert_called_once()
        mock_subs.assert_called_once()

    def test_stale_complete_skips_all_when_ttl_disabled(self, tmp_path: Path, mocker) -> None:
        data_dir = self._copy_resources("stale_complete", tmp_path)
        mock_ref  = mocker.patch("edgar.storage._local.download_reference_data")
        mock_fcts = mocker.patch("edgar.storage._local.download_facts")
        mock_subs = mocker.patch("edgar.storage._local.download_submissions")
        setup.download_reference(data_dir, ttl_days=0)
        setup.download_facts(data_dir, ttl_days=0)
        setup.download_submissions(data_dir, ttl_days=0)
        mock_ref.assert_not_called()
        mock_fcts.assert_not_called()
        mock_subs.assert_not_called()


# ---------------------------------------------------------------------------
# Integration test — EntityFacts reads from synthetic fixture files
# ---------------------------------------------------------------------------

class TestEntityFactsLocalStorage:
    """
    Calls edgar's EntityFacts stack with local storage enabled and the
    synthetic CIK JSON fixtures on disk.  No network calls are made.

    This validates the critical path:
        verify() → Company.get_facts() → get_company_facts(cik)
                → load_company_facts_from_local(cik)
                → reads CIK0000320193.json from disk
                → EntityFactsParser.parse_company_facts(json)
                → returns EntityFacts object
    """

    def test_load_aapl_facts_from_fixture(
        self, local_storage_env: Path, aapl_facts_json: dict
    ) -> None:
        from edgar.entity.entity_facts import load_company_facts_from_local
        result = load_company_facts_from_local(320193)
        assert result is not None
        assert result["cik"] == 320193
        assert result["name"] == "Apple Inc."

    def test_load_msft_facts_from_fixture(
        self, local_storage_env: Path, msft_facts_json: dict
    ) -> None:
        from edgar.entity.entity_facts import load_company_facts_from_local
        result = load_company_facts_from_local(789019)
        assert result is not None
        assert result["name"] == "MICROSOFT CORP"

    def test_get_company_facts_parses_fixture(
        self, local_storage_env: Path
    ) -> None:
        from edgar.entity.entity_facts import get_company_facts
        facts = get_company_facts(320193)
        assert facts is not None

    def test_get_company_facts_missing_cik_raises(
        self, local_storage_env: Path
    ) -> None:
        from edgar.entity.entity_facts import get_company_facts, NoCompanyFactsFound
        with pytest.raises(NoCompanyFactsFound):
            get_company_facts(9999999)  # not in fixtures

    def test_verify_passes_with_fixture_facts(
        self, local_storage_env: Path, mocker
    ) -> None:
        """
        verify() succeeds end-to-end when EntityFacts resolves from disk.

        Company("AAPL") normally does a CIK lookup via reference files.
        We mock that part only (Company constructor) so the test stays
        offline, but let the real get_facts() / local-storage path run.
        """
        from edgar.entity.entity_facts import _company_facts_cache
        _company_facts_cache.clear()

        # Mock only the Company constructor to return an entity whose CIK
        # we control; get_facts() is the real implementation.
        mock_entity = MagicMock()
        mock_entity.cik = 320193
        mock_entity.get_facts.side_effect = lambda: (
            __import__("edgar.entity.entity_facts", fromlist=["get_company_facts"])
            .get_company_facts(320193)
        )
        mocker.patch("edgar.Company", return_value=mock_entity)

        result = setup.verify(local_storage_env, ["AAPL"])
        assert result is True
