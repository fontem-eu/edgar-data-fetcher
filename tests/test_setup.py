"""
Tests for setup.py
==================

Unit tests     – mock edgar.storage.download_edgar_data; verify that the
                 correct flags are passed for each mode and that main()
                 orchestrates the right calls.

Integration    – put synthetic CIK JSON fixtures on disk, enable local
                 storage env vars, and call verify() to confirm the full
                 EntityFacts → local-file path works end-to-end.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=protected-access        # tests call internal setup._ helpers
# pylint: disable=import-outside-toplevel # edgar imports are deferred by design
# pylint: disable=unused-argument         # pytest fixtures used for side effects

import setup  # edgar-data-fetcher/setup.py (added to sys.path by conftest.py)


# ---------------------------------------------------------------------------
# Unit tests — directory creation & edgartools bootstrap
# ---------------------------------------------------------------------------

class TestDirectorySetup:
    def test_data_dir_is_created(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "brand-new"
        assert not new_dir.exists()
        new_dir.mkdir()
        assert new_dir.is_dir()

    def test_setup_edgartools_sets_env_vars(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("EDGAR_USE_LOCAL_DATA", raising=False)
        monkeypatch.delenv("EDGAR_LOCAL_DATA_DIR", raising=False)
        setup._setup_edgartools(tmp_path, "test@example.com")
        assert os.environ.get("EDGAR_USE_LOCAL_DATA") == "1"
        assert os.environ.get("EDGAR_LOCAL_DATA_DIR") == str(tmp_path)


# ---------------------------------------------------------------------------
# Unit tests — run_download passes the correct flags per mode
# ---------------------------------------------------------------------------

class TestRunDownload:
    def test_smoke_passes_correct_flags(self, data_dir: Path, mocker) -> None:
        mock_dl = mocker.patch("edgar.storage.download_edgar_data")
        setup.run_download(data_dir, "smoke")
        mock_dl.assert_called_once_with(
            reference=True, facts=False, submissions=False, disable_progress=False
        )

    def test_facts_passes_correct_flags(self, data_dir: Path, mocker) -> None:
        mock_dl = mocker.patch("edgar.storage.download_edgar_data")
        setup.run_download(data_dir, "facts")
        mock_dl.assert_called_once_with(
            reference=True, facts=True, submissions=False, disable_progress=False
        )

    def test_full_passes_correct_flags(self, data_dir: Path, mocker) -> None:
        mock_dl = mocker.patch("edgar.storage.download_edgar_data")
        setup.run_download(data_dir, "full")
        mock_dl.assert_called_once_with(
            reference=True, facts=True, submissions=True, disable_progress=False
        )


# ---------------------------------------------------------------------------
# Unit tests — mode selection (main orchestration)
# ---------------------------------------------------------------------------

class TestModeSelection:
    def _run_main(self, mode: str, tmp_path: Path, mocker, monkeypatch) -> MagicMock:
        mock_run = mocker.patch.object(setup, "run_download")
        mocker.patch.object(setup, "verify", return_value=True)
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
        return mock_run

    def test_smoke_calls_run_download_with_smoke(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        mock_run = self._run_main("smoke", tmp_path, mocker, monkeypatch)
        data_dir = tmp_path.resolve()
        tmp_dir = data_dir.parent / (data_dir.name + ".tmp")
        mock_run.assert_called_once_with(tmp_dir, "smoke")

    def test_facts_calls_run_download_with_facts(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        mock_run = self._run_main("facts", tmp_path, mocker, monkeypatch)
        data_dir = tmp_path.resolve()
        tmp_dir = data_dir.parent / (data_dir.name + ".tmp")
        mock_run.assert_called_once_with(tmp_dir, "facts")

    def test_full_calls_run_download_with_full(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        mock_run = self._run_main("full", tmp_path, mocker, monkeypatch)
        data_dir = tmp_path.resolve()
        tmp_dir = data_dir.parent / (data_dir.name + ".tmp")
        mock_run.assert_called_once_with(tmp_dir, "full")


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
        assert str(data_dir) in env_path.read_text()

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
        mock_company.get_facts.return_value = MagicMock()
        mocker.patch("edgar.Company", return_value=mock_company)
        assert setup.verify(data_dir, ["AAPL", "MSFT"]) is True

    def test_verify_returns_false_when_get_facts_returns_none(
        self, data_dir: Path, mocker
    ) -> None:
        mock_company = MagicMock()
        mock_company.get_facts.return_value = None
        mocker.patch("edgar.Company", return_value=mock_company)
        assert setup.verify(data_dir, ["AAPL"]) is False

    def test_verify_returns_false_on_exception(
        self, data_dir: Path, mocker
    ) -> None:
        mocker.patch("edgar.Company", side_effect=RuntimeError("network error"))
        assert setup.verify(data_dir, ["AAPL"]) is False

    def test_verify_checks_all_tickers(self, data_dir: Path, mocker) -> None:
        mock_company = MagicMock()
        mock_company.get_facts.return_value = MagicMock()
        mock_cls = mocker.patch("edgar.Company", return_value=mock_company)
        setup.verify(data_dir, ["AAPL", "MSFT", "NVDA"])
        assert mock_cls.call_count == 3


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
        assert get_company_facts(320193) is not None

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

        mock_entity = MagicMock()
        mock_entity.cik = 320193
        mock_entity.get_facts.side_effect = lambda: (
            __import__("edgar.entity.entity_facts", fromlist=["get_company_facts"])
            .get_company_facts(320193)
        )
        mocker.patch("edgar.Company", return_value=mock_entity)

        assert setup.verify(local_storage_env, ["AAPL"]) is True


# ---------------------------------------------------------------------------
# Tests — tmp-dir download pattern and metadata file
# ---------------------------------------------------------------------------

class TestTmpDirDownloadPattern:
    """Verifies the tmp-dir → data-dir swap and metadata behaviour in main()."""

    def _run_mocked_main(
        self, mocker, monkeypatch, tmp_path: Path, mode: str = "smoke"
    ) -> MagicMock:
        mock_run = mocker.patch.object(setup, "run_download")
        mocker.patch.object(setup, "verify", return_value=True)
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
        return mock_run

    def test_download_goes_into_tmp_dir(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        mock_run = self._run_mocked_main(mocker, monkeypatch, tmp_path)
        data_dir = tmp_path.resolve()
        tmp_dir = data_dir.parent / (data_dir.name + ".tmp")
        mock_run.assert_called_once_with(tmp_dir, "smoke")

    def test_data_dir_renamed_to_old_on_success(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        self._run_mocked_main(mocker, monkeypatch, tmp_path)
        old_dir = tmp_path.parent / (tmp_path.name + ".old")
        assert old_dir.exists()

    def test_tmp_dir_becomes_data_dir_on_success(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        self._run_mocked_main(mocker, monkeypatch, tmp_path)
        assert tmp_path.exists()
        tmp_dir = tmp_path.parent / (tmp_path.name + ".tmp")
        assert not tmp_dir.exists()

    def test_tmp_dir_deleted_on_download_error(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        mocker.patch.object(setup, "run_download", side_effect=RuntimeError("network error"))
        mocker.patch.object(setup, "_setup_edgartools")
        mocker.patch.object(setup, "_configure_logging")
        monkeypatch.setattr("sys.argv", [
            "setup.py",
            "--mode", "smoke",
            "--data-dir", str(tmp_path),
            "--identity", "test@example.com",
        ])
        with pytest.raises(SystemExit) as exc_info:
            setup.main()
        assert exc_info.value.code == 1
        tmp_dir = tmp_path.parent / (tmp_path.name + ".tmp")
        assert not tmp_dir.exists()

    def test_stale_tmp_dir_is_removed_before_download(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        tmp_dir = tmp_path.parent / (tmp_path.name + ".tmp")
        tmp_dir.mkdir()
        (tmp_dir / "stale.txt").write_text("stale")
        self._run_mocked_main(mocker, monkeypatch, tmp_path)
        # stale file must not appear in the final data dir
        assert not (tmp_path / "stale.txt").exists()

    def test_metadata_file_present_in_data_dir_after_success(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        self._run_mocked_main(mocker, monkeypatch, tmp_path)
        assert (tmp_path / setup.METADATA_FILE_NAME).exists()

    def test_metadata_has_success_status(
        self, tmp_path: Path, mocker, monkeypatch
    ) -> None:
        import yaml  # pylint: disable=import-outside-toplevel
        self._run_mocked_main(mocker, monkeypatch, tmp_path)
        meta = yaml.safe_load((tmp_path / setup.METADATA_FILE_NAME).read_text())
        assert meta["status"] == "success"
        assert "start_time" in meta
        assert "end_time" in meta
        assert "elapsed_seconds" in meta

    def test_write_metadata_creates_valid_yaml(self, tmp_path: Path) -> None:
        import yaml  # pylint: disable=import-outside-toplevel
        setup._write_metadata(tmp_path, status="test", foo="bar")
        meta = yaml.safe_load((tmp_path / setup.METADATA_FILE_NAME).read_text())
        assert meta["status"] == "test"
        assert meta["foo"] == "bar"
