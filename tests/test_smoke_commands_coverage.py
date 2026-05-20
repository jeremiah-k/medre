"""Cover temp-storage creation path in _run_session (lines 135-145 of smoke_commands.py).

When ``storage_path`` is None, ``_run_session`` creates a temporary SQLite
file with a ``medre-session-`` prefix and ``.db`` suffix, then passes it to
``run_bridge_session``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestRunSessionTempStorage:
    """Verify _run_session creates a temp SQLite file when no storage_path given."""

    @pytest.mark.asyncio
    async def test_temp_storage_created_when_none(self) -> None:
        """storage_path=None triggers temp file creation with correct prefix/suffix."""
        from medre.cli.smoke_commands import _run_session

        fake_report = {
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

        with patch(
            "medre.runtime.run_session.orchestration.run_bridge_session",
            new_callable=AsyncMock,
            return_value=fake_report,
        ) as mock_bridge:
            with pytest.raises(SystemExit) as exc_info:
                await _run_session(
                    config_path=None,
                    storage_path=None,
                    snapshot_dir=None,
                    json_output=False,
                )

            assert exc_info.value.code == 0

            # run_bridge_session was called once
            mock_bridge.assert_awaited_once()

            # The storage_path kwarg should be a temp file with correct naming
            call_kwargs = mock_bridge.call_args
            passed_path = call_kwargs.kwargs["storage_path"]
            assert "medre-session-" in passed_path
            assert passed_path.endswith(".db")

            # Clean up temp file if it still exists
            import os

            if os.path.exists(passed_path):
                os.unlink(passed_path)

    @pytest.mark.asyncio
    async def test_temp_storage_passed_to_bridge_session(self) -> None:
        """The generated temp path is forwarded as storage_path to run_bridge_session."""
        from medre.cli.smoke_commands import _run_session

        fake_report = {
            "status": "passed",
            "event_id": "evt-test-2",
            "route_id": "route-2",
            "source_adapter": "fake-src",
            "target_adapters": [],
            "accounting": None,
            "delivery_receipts": [],
            "native_refs": [],
            "final_snapshot_checks": None,
            "commands": None,
            "storage_path": "/tmp/medre-session-fake2.db",
            "final_snapshot_path": None,
        }

        with patch(
            "medre.runtime.run_session.orchestration.run_bridge_session",
            new_callable=AsyncMock,
            return_value=fake_report,
        ) as mock_bridge:
            with pytest.raises(SystemExit):
                await _run_session(
                    config_path=None,
                    storage_path=None,
                    snapshot_dir=None,
                    json_output=False,
                )

            passed_path = mock_bridge.call_args.kwargs["storage_path"]

            # Must be an absolute path to a .db file
            assert passed_path.startswith("/")
            assert passed_path.endswith(".db")

            # File was actually created on disk by NamedTemporaryFile
            import os

            assert os.path.exists(passed_path)

            # Cleanup
            os.unlink(passed_path)

    @pytest.mark.asyncio
    async def test_no_temp_storage_when_path_provided(self) -> None:
        """When storage_path is given, no temp file is created."""
        from medre.cli.smoke_commands import _run_session

        explicit_path = "/tmp/test-explicit-storage.db"
        fake_report = {
            "status": "passed",
            "event_id": "evt-test-3",
            "route_id": "route-3",
            "source_adapter": "fake-src",
            "target_adapters": [],
            "accounting": None,
            "delivery_receipts": [],
            "native_refs": [],
            "final_snapshot_checks": None,
            "commands": None,
            "storage_path": explicit_path,
            "final_snapshot_path": None,
        }

        with patch(
            "medre.runtime.run_session.orchestration.run_bridge_session",
            new_callable=AsyncMock,
            return_value=fake_report,
        ) as mock_bridge:
            with pytest.raises(SystemExit):
                await _run_session(
                    config_path=None,
                    storage_path=explicit_path,
                    snapshot_dir=None,
                    json_output=False,
                )

            # The explicit path is passed through unchanged
            assert mock_bridge.call_args.kwargs["storage_path"] == explicit_path
