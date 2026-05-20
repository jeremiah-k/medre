"""Cover temp-storage creation path in _run_session (lines 135-145 of smoke_commands.py).

When ``storage_path`` is None, ``_run_session`` creates a temporary SQLite
file with a ``medre-session-`` prefix and ``.db`` suffix, then passes it to
``run_bridge_session``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _fake_report(**overrides: object) -> dict[str, object]:
    """Minimal report dict that satisfies _run_session's exit-0 path."""
    base: dict[str, object] = {
        "status": "passed",
        "event_id": "evt-test",
        "route_id": "route-1",
        "source_adapter": "fake-src",
        "target_adapters": ["fake-dst"],
        "accounting": {},
        "delivery_receipts": [],
        "native_refs": [],
        "final_snapshot_checks": {},
        "commands": {},
        "storage_path": "/tmp/medre-session-fake.db",
        "final_snapshot_path": None,
    }
    base.update(overrides)
    return base


class TestRunSessionTempStorage:
    """Verify _run_session creates a temp SQLite file when no storage_path given."""

    @pytest.mark.asyncio
    async def test_temp_storage_created_when_none(self) -> None:
        """storage_path=None triggers temp file creation with correct prefix/suffix."""
        from medre.cli.smoke_commands import _run_session

        with patch(
            "medre.runtime.run_session.orchestration.run_bridge_session",
            new_callable=AsyncMock,
            return_value=_fake_report(),
        ) as mock_bridge:
            with pytest.raises(SystemExit) as exc_info:
                await _run_session(
                    config_path=None,
                    storage_path=None,
                    snapshot_dir=None,
                    json_output=False,
                )

            assert exc_info.value.code == 0
            mock_bridge.assert_awaited_once()

            passed_path = mock_bridge.call_args.kwargs["storage_path"]
            try:
                assert "medre-session-" in passed_path
                assert passed_path.endswith(".db")
            finally:
                if os.path.exists(passed_path):
                    os.unlink(passed_path)

    @pytest.mark.asyncio
    async def test_temp_storage_passed_to_bridge_session(self) -> None:
        """The generated temp path is forwarded as storage_path to run_bridge_session."""
        from medre.cli.smoke_commands import _run_session

        with patch(
            "medre.runtime.run_session.orchestration.run_bridge_session",
            new_callable=AsyncMock,
            return_value=_fake_report(),
        ) as mock_bridge:
            with pytest.raises(SystemExit) as exc_info:
                await _run_session(
                    config_path=None,
                    storage_path=None,
                    snapshot_dir=None,
                    json_output=False,
                )

            assert exc_info.value.code == 0
            passed_path = mock_bridge.call_args.kwargs["storage_path"]

            try:
                assert passed_path.startswith("/")
                assert passed_path.endswith(".db")
                assert os.path.exists(passed_path)
            finally:
                if os.path.exists(passed_path):
                    os.unlink(passed_path)

    @pytest.mark.asyncio
    async def test_no_temp_storage_when_path_provided(self, tmp_path: Path) -> None:
        """When storage_path is given, no temp file is created."""
        from medre.cli.smoke_commands import _run_session

        explicit_path = str(tmp_path / "test-explicit-storage.db")

        with patch(
            "medre.runtime.run_session.orchestration.run_bridge_session",
            new_callable=AsyncMock,
            return_value=_fake_report(storage_path=explicit_path),
        ) as mock_bridge:
            with pytest.raises(SystemExit) as exc_info:
                await _run_session(
                    config_path=None,
                    storage_path=explicit_path,
                    snapshot_dir=None,
                    json_output=False,
                )

            assert exc_info.value.code == 0
            assert mock_bridge.call_args.kwargs["storage_path"] == explicit_path
