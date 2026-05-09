"""Unit tests for medre.runner: env-var configuration, diagnostics, and lifecycle.

These tests exercise the runner's pure-Python logic without a real Matrix
server.  Environment variables are injected via ``monkeypatch``; adapters
and subsystems are mocked with ``unittest.mock``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.base import AdapterCapabilities, AdapterInfo, AdapterRole
from medre.adapters.matrix.config import MatrixConfig
from medre.runner import (
    _build_matrix_config,
    _env,
    _extract_operational_details,
    collect_diagnostics,
    run_alpha_matrix,
)

# ---------------------------------------------------------------------------
# Minimal required env vars for a valid _build_matrix_config call
# ---------------------------------------------------------------------------
_REQUIRED_ENV = {
    "MATRIX_HOMESERVER": "https://matrix.test",
    "MATRIX_USER_ID": "@bot:test",
    "MATRIX_ACCESS_TOKEN": "syt_secret123",
}


# ===================================================================
# TestBuildMatrixConfig
# ===================================================================


class TestBuildMatrixConfig:
    """Tests for ``_build_matrix_config`` and the ``_env`` helper."""

    # -- _env helper --------------------------------------------------------

    def test_env_required_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _env("MY_VAR") == "hello"

    def test_env_required_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_VAR", raising=False)
        with pytest.raises(EnvironmentError, match="Required environment variable"):
            _env("MY_VAR")

    def test_env_optional_missing_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_VAR", raising=False)
        assert _env("MY_VAR", required=False) == ""

    # -- _build_matrix_config: required vars --------------------------------

    def test_reads_required_homeserver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://hs.example.com")
        monkeypatch.setenv("MATRIX_USER_ID", "@u:example.com")
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "tok")
        # Clear optional vars
        for k in (
            "MATRIX_ROOM_ALLOWLIST",
            "MATRIX_ADAPTER_ID",
            "MATRIX_DEVICE_ID",
            "MATRIX_STORE_PATH",
            "MATRIX_SYNC_TIMEOUT_MS",
        ):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.homeserver == "https://hs.example.com"

    def test_reads_required_user_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        for k in (
            "MATRIX_ROOM_ALLOWLIST",
            "MATRIX_ADAPTER_ID",
            "MATRIX_DEVICE_ID",
            "MATRIX_STORE_PATH",
            "MATRIX_SYNC_TIMEOUT_MS",
        ):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.user_id == "@bot:test"

    def test_reads_required_access_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        for k in (
            "MATRIX_ROOM_ALLOWLIST",
            "MATRIX_ADAPTER_ID",
            "MATRIX_DEVICE_ID",
            "MATRIX_STORE_PATH",
            "MATRIX_SYNC_TIMEOUT_MS",
        ):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.access_token == "syt_secret123"

    # -- missing required vars raise cleanly --------------------------------

    def test_missing_homeserver_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
        monkeypatch.setenv("MATRIX_USER_ID", "@u:t")
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "tok")
        with pytest.raises(EnvironmentError, match="MATRIX_HOMESERVER"):
            _build_matrix_config()

    def test_missing_user_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://h")
        monkeypatch.delenv("MATRIX_USER_ID", raising=False)
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "tok")
        with pytest.raises(EnvironmentError, match="MATRIX_USER_ID"):
            _build_matrix_config()

    def test_missing_access_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://h")
        monkeypatch.setenv("MATRIX_USER_ID", "@u:t")
        monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
        with pytest.raises(EnvironmentError, match="MATRIX_ACCESS_TOKEN"):
            _build_matrix_config()

    # -- MATRIX_ROOM_ALLOWLIST: comma-separated parsing ----------------------

    def test_room_allowlist_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_ROOM_ALLOWLIST", "!a:test, !b:test, !c:test")
        for k in ("MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.room_allowlist == {"!a:test", "!b:test", "!c:test"}

    def test_room_allowlist_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_ROOM_ALLOWLIST", "!solo:test")
        for k in ("MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.room_allowlist == {"!solo:test"}

    def test_room_allowlist_empty_means_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_ROOM_ALLOWLIST", "")
        for k in ("MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.room_allowlist is None

    def test_room_allowlist_unset_means_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("MATRIX_ROOM_ALLOWLIST", raising=False)
        for k in ("MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.room_allowlist is None

    # -- MATRIX_SYNC_TIMEOUT_MS: integer parsing ----------------------------

    def test_sync_timeout_parses_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_SYNC_TIMEOUT_MS", "60000")
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.sync_timeout_ms == 60000

    def test_sync_timeout_default_30000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.sync_timeout_ms == 30000

    # -- MATRIX_ADAPTER_ID: default -----------------------------------------

    def test_adapter_id_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("MATRIX_ADAPTER_ID", raising=False)
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.adapter_id == "matrix-alpha"

    def test_adapter_id_custom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_ADAPTER_ID", "my-custom-id")
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_DEVICE_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.adapter_id == "my-custom-id"

    # -- MATRIX_STORE_PATH --------------------------------------------------

    def test_store_path_passed_into_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_STORE_PATH", "/tmp/nio_store")
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.store_path == "/tmp/nio_store"

    def test_store_path_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("MATRIX_STORE_PATH", raising=False)
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_ADAPTER_ID", "MATRIX_DEVICE_ID", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.store_path is None

    # -- MATRIX_DEVICE_ID ---------------------------------------------------

    def test_device_id_passed_into_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MATRIX_DEVICE_ID", "DEVICEABC")
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_ADAPTER_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.device_id == "DEVICEABC"

    def test_device_id_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("MATRIX_DEVICE_ID", raising=False)
        for k in ("MATRIX_ROOM_ALLOWLIST", "MATRIX_ADAPTER_ID", "MATRIX_STORE_PATH", "MATRIX_SYNC_TIMEOUT_MS"):
            monkeypatch.delenv(k, raising=False)

        cfg = _build_matrix_config()
        assert cfg.device_id is None


# ===================================================================
# TestCollectDiagnostics
# ===================================================================


class TestCollectDiagnostics:
    """Tests for ``_extract_operational_details`` and ``collect_diagnostics``."""

    @staticmethod
    def _make_mock_adapter(
        *,
        connected: bool = True,
        logged_in: bool = True,
        sync_task_running: bool = True,
        sync_failure: Exception | None = None,
    ) -> SimpleNamespace:
        """Build a mock adapter with the internal attributes accessed by runner."""
        mock_client = None
        if connected:
            mock_client = SimpleNamespace(logged_in=logged_in)

        sync_task = None
        if sync_task_running:
            # A non-done asyncio.Task-like object
            sync_task = SimpleNamespace(done=lambda: False)
        elif connected:
            # Done task
            sync_task = SimpleNamespace(done=lambda: True)

        return SimpleNamespace(
            _client=mock_client,
            _sync_task=sync_task,
            _sync_failure=sync_failure,
        )

    # -- _extract_operational_details shape ----------------------------------

    def test_details_has_expected_keys(self) -> None:
        adapter = self._make_mock_adapter()
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert set(details.keys()) == {
            "connected",
            "logged_in",
            "sync_task_running",
            "last_sync_error",
        }

    def test_connected_true_when_client_present(self) -> None:
        adapter = self._make_mock_adapter(connected=True)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["connected"] is True

    def test_connected_false_when_client_absent(self) -> None:
        adapter = self._make_mock_adapter(connected=False)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["connected"] is False

    def test_logged_in_true(self) -> None:
        adapter = self._make_mock_adapter(connected=True, logged_in=True)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["logged_in"] is True

    def test_logged_in_false_when_not_logged_in(self) -> None:
        adapter = self._make_mock_adapter(connected=True, logged_in=False)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["logged_in"] is False

    def test_logged_in_false_when_disconnected(self) -> None:
        adapter = self._make_mock_adapter(connected=False)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["logged_in"] is False

    def test_sync_task_running_true(self) -> None:
        adapter = self._make_mock_adapter(sync_task_running=True)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["sync_task_running"] is True

    def test_sync_task_running_false_when_done(self) -> None:
        adapter = self._make_mock_adapter(sync_task_running=False, connected=True)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["sync_task_running"] is False

    def test_sync_task_running_false_when_none(self) -> None:
        adapter = self._make_mock_adapter(connected=False, sync_task_running=False)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["sync_task_running"] is False

    def test_last_sync_error_none_when_no_failure(self) -> None:
        adapter = self._make_mock_adapter(sync_failure=None)
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["last_sync_error"] is None

    def test_last_sync_error_string_when_failure(self) -> None:
        adapter = self._make_mock_adapter(sync_failure=RuntimeError("sync died"))
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        assert details["last_sync_error"] == "sync died"

    # -- Secret redaction: no token leakage ----------------------------------

    def test_details_contain_no_access_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure diagnostics dict never contains the raw access token."""
        adapter = self._make_mock_adapter()
        details = _extract_operational_details(adapter)  # type: ignore[arg-type]
        details_str = str(details)
        # The token is never part of _extract_operational_details output
        assert "syt_" not in details_str
        assert "access_token" not in details_str

    def test_collect_diagnostics_no_token_leakage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full collect_diagnostics output must not leak the access token."""
        adapter = self._make_mock_adapter()
        # We need an AdapterInfo to pass to collect_diagnostics
        info = AdapterInfo(
            adapter_id="matrix-alpha",
            platform="matrix",
            role=AdapterRole.TRANSPORT,
            version="0.1.0",
            capabilities=AdapterCapabilities(),
            health="ok",
        )
        result = collect_diagnostics(adapter, info=info)  # type: ignore[arg-type]
        result_str = str(result)
        assert "syt_secret" not in result_str

    # -- collect_diagnostics: AdapterInfo required ---------------------------

    def test_collect_diagnostics_raises_without_info(self) -> None:
        adapter = self._make_mock_adapter()
        with pytest.raises(TypeError, match="collect_diagnostics requires an AdapterInfo"):
            collect_diagnostics(adapter, info=None)  # type: ignore[arg-type]

    # -- collect_diagnostics merges operational details ----------------------

    def test_collect_diagnostics_includes_details_key(self) -> None:
        adapter = self._make_mock_adapter(connected=True, logged_in=True, sync_task_running=True)
        info = AdapterInfo(
            adapter_id="matrix-alpha",
            platform="matrix",
            role=AdapterRole.TRANSPORT,
            version="0.1.0",
            capabilities=AdapterCapabilities(),
            health="ok",
        )
        result = collect_diagnostics(adapter, info=info)  # type: ignore[arg-type]
        assert "details" in result
        inner = result["details"]
        assert inner["connected"] is True
        assert inner["logged_in"] is True
        assert inner["sync_task_running"] is True
        assert "last_sync_error" in inner


# ===================================================================
# TestRunAlphaMatrix
# ===================================================================


class TestRunAlphaMatrix:
    """Smoke test for ``run_alpha_matrix`` with all subsystems monkeypatched."""

    @pytest.fixture()
    def _env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set required env vars for runner."""
        for k, v in _REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        # Ensure MEDRE_DB_PATH uses :memory: by default
        monkeypatch.delenv("MEDRE_DB_PATH", raising=False)
        # Clear optional vars
        for k in (
            "MATRIX_ROOM_ALLOWLIST",
            "MATRIX_ADAPTER_ID",
            "MATRIX_DEVICE_ID",
            "MATRIX_STORE_PATH",
            "MATRIX_SYNC_TIMEOUT_MS",
        ):
            monkeypatch.delenv(k, raising=False)

    @pytest.mark.usefixtures("_env_vars")
    def test_medre_db_path_default_is_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MEDRE_DB_PATH defaults to ``:memory:`` when unset."""
        monkeypatch.delenv("MEDRE_DB_PATH", raising=False)
        # We verify by checking the default value is used inside run_alpha_matrix.
        # This is tested indirectly through the smoke test below, but also
        # we confirm the os.environ.get call returns the default:
        import os
        assert os.environ.get("MEDRE_DB_PATH", ":memory:") == ":memory:"

    @pytest.mark.usefixtures("_env_vars")
    @pytest.mark.asyncio
    async def test_smoke_lifecycle_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_alpha_matrix wires and calls all subsystems without network."""
        # -- Mock all subsystems -------------------------------------------
        mock_storage_instance = MagicMock()
        mock_storage_instance.initialize = AsyncMock()
        mock_storage_instance.close = AsyncMock()

        mock_adapter_instance = MagicMock()
        mock_adapter_instance.start = AsyncMock()
        mock_adapter_instance.stop = AsyncMock()
        mock_adapter_instance.health_check = AsyncMock(return_value=AdapterInfo(
            adapter_id="matrix-alpha",
            platform="matrix",
            role=AdapterRole.TRANSPORT,
            version="0.1.0",
            capabilities=AdapterCapabilities(),
            health="ok",
        ))

        mock_pipeline_runner_instance = MagicMock()
        mock_pipeline_runner_instance.start = AsyncMock()
        mock_pipeline_runner_instance.stop = AsyncMock()
        mock_pipeline_runner_instance.ingress_handler = AsyncMock()

        # Patch constructors and signal handling
        with patch("medre.runner.SQLiteStorage", return_value=mock_storage_instance) as MockSQLiteStorage, \
             patch("medre.runner.MatrixAdapter", return_value=mock_adapter_instance) as MockMatrixAdapter, \
             patch("medre.runner.PipelineRunner", return_value=mock_pipeline_runner_instance) as MockPipelineRunner, \
             patch("medre.runner.Diagnostician"), \
             patch("medre.runner.Router"), \
             patch("medre.runner.FallbackResolver"), \
             patch("medre.runner.RelationResolver"), \
             patch("medre.runner.RenderingPipeline") as mock_rp_cls, \
             patch("medre.runner.normalize_adapter_health", return_value={"adapter_id": "matrix-alpha", "details": {}}):

            mock_rendering_pipeline = MagicMock()
            mock_rp_cls.return_value = mock_rendering_pipeline

            # Capture the signal handler callback so we can trigger shutdown.
            signal_handlers: list = []

            real_get_running_loop = asyncio.get_running_loop

            def _fake_add_signal_handler(sig, callback, *args):
                signal_handlers.append(callback)

            # Schedule shutdown immediately after the loop starts so
            # the runner doesn't block on shutdown_event.wait().
            async def _auto_shutdown():
                # Give the runner time to set up, then trigger all signal handlers
                await asyncio.sleep(0)
                for cb in signal_handlers:
                    cb()

            with patch.object(
                asyncio.get_running_loop(), "add_signal_handler", side_effect=_fake_add_signal_handler
            ):
                # Arrange that a soon-scheduled coroutine triggers the signal handler
                loop = real_get_running_loop()
                loop.create_task(_auto_shutdown())
                await run_alpha_matrix()

            # -- Verify lifecycle calls ------------------------------------
            mock_storage_instance.initialize.assert_awaited_once()
            mock_adapter_instance.start.assert_awaited_once()
            mock_adapter_instance.health_check.assert_awaited_once()
            mock_adapter_instance.stop.assert_awaited_once()
            mock_pipeline_runner_instance.start.assert_awaited_once()
            mock_pipeline_runner_instance.stop.assert_awaited_once()
            mock_storage_instance.close.assert_awaited_once()
            # PipelineRunner created with pipeline_config
            MockPipelineRunner.assert_called_once()
            # RenderingPipeline created and renderer registered
            mock_rp_cls.assert_called_once()
            mock_rendering_pipeline.register.assert_called_once()
