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
        "storage_path": "medre-session-fake.db",
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
                assert os.path.isabs(passed_path)
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


async def test_run_session_human_output_lists_nested_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Human output renders both authoritative command-text sections."""
    from medre.cli.smoke_commands import _run_session
    from medre.runtime.run_session import orchestration

    storage_path = str(tmp_path / "session.db")
    report = _fake_report(
        storage_path=storage_path,
        commands={
            "commands_text": {
                "primary": {
                    "inspect_event": "medre inspect-event evt-test",
                },
                "specialized": {
                    "recover_event": "medre recover --event evt-test",
                },
            }
        },
    )

    async def _fake_run_bridge_session(
        *args: object, **kwargs: object
    ) -> dict[str, object]:
        return report

    monkeypatch.setattr(
        orchestration,
        "run_bridge_session",
        _fake_run_bridge_session,
    )

    with pytest.raises(SystemExit) as exc_info:
        await _run_session(
            config_path=None,
            storage_path=storage_path,
            snapshot_dir=None,
            json_output=False,
        )

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "  Commands:" in output
    assert "    inspect_event: medre inspect-event evt-test" in output
    assert "    recover_event: medre recover --event evt-test" in output


async def test_run_session_human_output_ignores_flat_command_text_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The removed flat command-text shape is not rendered."""
    from medre.cli.smoke_commands import _run_session
    from medre.runtime.run_session import orchestration

    storage_path = str(tmp_path / "session.db")
    report = _fake_report(
        storage_path=storage_path,
        commands={
            "commands_text": {
                "inspect_event": "medre inspect-event old-flat-shape",
            }
        },
    )

    async def _fake_run_bridge_session(
        *args: object, **kwargs: object
    ) -> dict[str, object]:
        return report

    monkeypatch.setattr(
        orchestration,
        "run_bridge_session",
        _fake_run_bridge_session,
    )

    with pytest.raises(SystemExit) as exc_info:
        await _run_session(
            config_path=None,
            storage_path=storage_path,
            snapshot_dir=None,
            json_output=False,
        )

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "  Commands:" not in output
    assert "old-flat-shape" not in output
