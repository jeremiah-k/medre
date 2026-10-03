"""Retain convergence evidence when a later radio operation fails."""

from __future__ import annotations

import asyncio
import importlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize(
    "failure", ["first_send", "first_cancel", "send", "listener", "cancel", "receipt"],
)
async def test_convergence_retains_peer_evidence_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    # Preserve the helpers' endpoint snapshots; no physical endpoint is opened.
    importlib.import_module("tests.helpers.meshcore_live_peer")
    importlib.import_module("tests.helpers.meshtastic_live_peer")
    namespace = runpy.run_path(
        str(Path(__file__).with_name("test_radio_message_matrix_live.py"))
    )
    case = namespace["test_sustained_traffic_convergence"]
    globals_ = case.__globals__
    packets = [{"text": "observed-first-direction", "timestamp": 123, "path_len": 0}]
    accepted = [{"sent_text": "V-MT-0", "sent_id": 123}]

    class McListener:
        def __init__(self, seconds: float) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def packets_until(self, predicate, timeout: float) -> list[dict]:
            return packets

    class MtListener(McListener):
        def __enter__(self):
            if failure == "listener":
                raise RuntimeError("second listener failed")
            return self

        def packets_until(self, predicate, timeout: float) -> list[dict]:
            return []

    cancelled = failure in {"cancel", "first_cancel"}
    error = asyncio.CancelledError() if cancelled else RuntimeError("send failed")
    first_failed = failure in {"first_send", "first_cancel"}
    mt_send = AsyncMock(side_effect=error) if first_failed else AsyncMock(return_value=accepted)
    mc_accepted = [{"sent_text": "V-MC-0", "timestamp": 456}]
    mc_send = AsyncMock(return_value=mc_accepted) if failure == "receipt" else AsyncMock(side_effect=error)
    stop = AsyncMock()
    for key, value in {
        "_launch": AsyncMock(return_value=SimpleNamespace(stop=stop)),
        "_McListener": McListener,
        "_MtListener": MtListener,
        "_mt_send": mt_send,
        "_mc_send": mc_send,
        "_await_events_with_nonce": AsyncMock(return_value=[]),
        "_TRAFFIC": 2,
        "_nonce": lambda label: label,
        "asyncio": SimpleNamespace(sleep=AsyncMock()),
    }.items():
        monkeypatch.setitem(globals_, key, value)

    expected_error = asyncio.CancelledError if cancelled else (
        AssertionError if failure == "receipt" else RuntimeError
    )
    with pytest.raises(expected_error):
        await case(tmp_path)

    evidence = json.loads((tmp_path / "peer-observations.json").read_text())
    assert evidence["meshtastic_source"] == {
        "expected": ["V-MT-0"], "accepted": None if first_failed else accepted,
    }
    assert evidence["meshcore_source"]["accepted"] == (
        mc_accepted if failure == "receipt" else None
    )
    assert evidence["meshcore_peer_texts"] == ["observed-first-direction"]
    assert evidence["meshcore_peer_packets"] == packets
    assert evidence["meshtastic_peer_texts"] == []
    stop.assert_awaited_once()
