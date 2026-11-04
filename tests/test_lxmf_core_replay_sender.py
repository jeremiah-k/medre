# medre-test-tier: compatibility
"""Success evidence requires clean native receiver teardown and an exact schema."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from tests.helpers import lxmf_core_replay_sender as sender


@pytest.mark.parametrize(
    ("exit_code", "delivery_count", "timeout"),
    [(0, 1, False), (3, 1, False), (0, 2, False), (-9, 1, True)],
)
async def test_receiver_teardown_guards_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
    delivery_count: int,
    timeout: bool,
) -> None:
    (tmp_path / "b-ready.json").write_text(
        json.dumps({"delivery_hash": "target", "propagation_hash": "propagation"}),
        encoding="utf-8",
    )
    verdict_path = tmp_path / "recipient-verdict.json"
    replay = AsyncMock(return_value={"native_evidence_fingerprint": "a" * 64})
    monkeypatch.setattr(sender, "_replay_core", replay)
    monkeypatch.setattr(sender, "_ports", lambda: (12345, 12346))
    monkeypatch.setattr(sender.os, "environ", dict(sender.os.environ))

    async def wait() -> int:
        assert not verdict_path.exists(), "success cannot precede receiver teardown"
        (tmp_path / "b-received.json").write_text(
            json.dumps({"received": True, "delivery_count": delivery_count}),
            encoding="utf-8",
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
    if (exit_code, delivery_count, timeout) == (0, 1, False):
        await sender._main(tmp_path, tmp_path / "receiver.py")
        published = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert set(published) == {
            "received",
            "delivery_count",
            "body_matches",
            "native_id_matches",
            "destination_matches",
            "evidence_fingerprint",
        }
        assert published["evidence_fingerprint"] == "a" * 64
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


def test_recipient_artifact_upload_requires_successful_qualification() -> None:
    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/lxmf-core-native-qualification.yml"
        ).read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["core-lxmf-replay"]["steps"]
    recipient = [
        step
        for step in steps
        if step.get("name") == "Upload verified recipient evidence"
    ]
    assert len(recipient) == 1
    assert recipient[0]["if"] == "success()"
    assert "recipient-verdict.json" in recipient[0]["with"]["path"]
    diagnostic = [
        step
        for step in steps
        if step.get("name") == "Upload native qualification diagnostics"
    ]
    assert len(diagnostic) == 1
    assert diagnostic[0]["if"] == "always()"
    assert "recipient-verdict.json" not in diagnostic[0]["with"]["path"]
