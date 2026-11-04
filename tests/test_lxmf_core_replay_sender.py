# medre-test-tier: compatibility
"""Receiver teardown must finish before native replay evidence is published."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.helpers import lxmf_core_replay_sender as sender


@pytest.mark.parametrize(
    ("exit_code", "delivery_count", "timeout"),
    [(0, 1, False), (3, 1, False), (0, 2, False), (-9, 1, True)],
)
async def test_only_clean_receiver_teardown_publishes_a_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
    delivery_count: int,
    timeout: bool,
) -> None:
    (tmp_path / "b-ready.json").write_text(
        json.dumps({"delivery_hash": "target", "propagation_hash": "propagation"})
    )
    verdict_path = tmp_path / "recipient-verdict.json"
    replay = AsyncMock(return_value={"native_evidence_fingerprint": "a" * 64})
    monkeypatch.setattr(sender, "_replay_core", replay)
    monkeypatch.setattr(sender, "_ports", lambda: (12345, 12346))
    monkeypatch.setattr(sender.os, "environ", dict(sender.os.environ))

    async def wait() -> int:
        assert not verdict_path.exists(), "success must wait for receiver teardown"
        (tmp_path / "b-received.json").write_text(
            json.dumps({"received": True, "delivery_count": delivery_count})
        )
        return exit_code

    child = SimpleNamespace(
        returncode=exit_code,
        wait=AsyncMock(side_effect=wait),
        kill=Mock(),
    )
    if timeout:
        child.wait.side_effect = [TimeoutError(), exit_code]
    monkeypatch.setattr(
        sender.asyncio, "create_subprocess_exec", AsyncMock(return_value=child)
    )
    if exit_code == 0 and delivery_count == 1 and not timeout:
        await sender._main(tmp_path, tmp_path / "receiver.py")
        published = json.loads(verdict_path.read_text())
        assert published["received"] is True
        assert published["delivery_count"] == 1
        assert published["evidence_fingerprint"] == "a" * 64
        assert "body" not in published and "title" not in published
        assert sender._RESULT_PREFIX in capsys.readouterr().out
    else:
        with pytest.raises(AssertionError, match="receiver"):
            await sender._main(tmp_path, tmp_path / "receiver.py")
        assert not verdict_path.exists()
        assert sender._RESULT_PREFIX not in capsys.readouterr().out
    assert (tmp_path / "stop").is_file()
    replay.assert_awaited_once_with(tmp_path, "target", "propagation")
    if timeout:
        child.kill.assert_called_once_with()
        assert child.wait.await_count == 2
    else:
        child.kill.assert_not_called()
