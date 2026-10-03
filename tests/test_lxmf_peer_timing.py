"""Slow-medium and finite-deadline behavior of the native LXMF peer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.helpers import lxmf_peer_timing as timing


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "bad"])
def test_delivery_budget_rejects_invalid_overrides(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LXMF_PEER_DELIVERY_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError):
        timing.delivery_timeout_seconds()


def test_parent_budget_covers_all_messages_and_startup(monkeypatch) -> None:
    monkeypatch.setenv("LXMF_PEER_DELIVERY_TIMEOUT_SECONDS", "180")
    assert timing.delivery_timeout_seconds() == 180
    assert timing.send_timeout_seconds(3) == 40 + 45 + 3 * (180 + 2.5) + 10


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_parent_budget_rejects_invalid_message_counts(count) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        timing.send_timeout_seconds(count)


def _clock(monkeypatch, message, *, delivered_at: float | None = None):
    clock = SimpleNamespace(now=0.0, sleeps=[])
    monkeypatch.setattr(timing.time, "monotonic", lambda: clock.now)

    def advance(duration: float) -> None:
        clock.sleeps.append(duration)
        clock.now += duration
        if delivered_at is not None and clock.now >= delivered_at:
            message.state = 8

    monkeypatch.setattr(timing.time, "sleep", advance)
    return clock


def test_configured_budget_allows_delivery_after_the_bench_window(monkeypatch) -> None:
    monkeypatch.setenv("LXMF_PEER_DELIVERY_TIMEOUT_SECONDS", "180")
    message = SimpleNamespace(state=2)
    clock = _clock(monkeypatch, message, delivered_at=120)
    state = timing.wait_terminal(message, (8, 255), timing.delivery_timeout_seconds())
    assert state == 8
    assert clock.now == 120


def test_pending_delivery_is_bounded_without_fabricating_a_failure(monkeypatch) -> None:
    message = SimpleNamespace(state=2)
    clock = _clock(monkeypatch, message)
    assert timing.wait_terminal(message, (8, 255), 0.6) == 2
    assert clock.now == pytest.approx(0.6)
    assert clock.sleeps[-1] == pytest.approx(0.1)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf"), -1])
def test_terminal_wait_rejects_unbounded_or_negative_budgets(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout must be finite and >= 0"):
        timing.wait_terminal(SimpleNamespace(state=2), (8, 255), timeout)


def test_zero_budget_reports_the_state_without_waiting(monkeypatch) -> None:
    message = SimpleNamespace(state=2)
    clock = _clock(monkeypatch, message)
    assert timing.wait_terminal(message, (8, 255), 0) == 2
    assert clock.sleeps == []


@pytest.mark.parametrize("state", [8, 253, 254, 255])
def test_all_terminal_states_return_without_waiting(monkeypatch, state: int) -> None:
    message = SimpleNamespace(state=state)
    clock = _clock(monkeypatch, message)
    assert timing.wait_terminal(message, (8, 253, 254, 255), 180) == state
    assert clock.sleeps == []


@pytest.mark.parametrize("mode", ["send", "sendenv"])
def test_native_child_uses_the_parent_ceiling_and_reports_the_observed_state(
    monkeypatch, capsys, mode: str,
) -> None:
    """Exercise child argv parsing and JSON output with SDK boundary doubles."""
    import json
    import sys
    import threading

    from tests.helpers.lxmf_live_peer import _PEER_SCRIPT

    class Message:
        DIRECT = 2
        DELIVERED = 8
        REJECTED = 253
        CANCELLED = 254
        FAILED = 255

        def __init__(self, destination, source, content, **kwargs):
            self.state = self.DIRECT
            self.hash = b"\x11" * 32
            _clock(monkeypatch, self, delivered_at=120)

    identity = SimpleNamespace(
        from_file=lambda _path: object(), recall=lambda _hash: object()
    )
    destination = Mock(return_value=object())
    destination.OUT = 1
    destination.SINGLE = 0
    router = SimpleNamespace(
        register_delivery_identity=lambda *args, **kwargs: object(),
        handle_outbound=lambda message: None,
    )
    monkeypatch.setitem(
        sys.modules, "RNS", SimpleNamespace(
            Reticulum=lambda **kwargs: object(), Identity=identity, Destination=destination,
        ),
    )
    monkeypatch.setitem(
        sys.modules, "LXMF",
        SimpleNamespace(LXMessage=Message, LXMRouter=lambda **kwargs: router),
    )
    monkeypatch.setattr(
        threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None)
    )
    args = [
        "peer", mode, "rns", "identity", "storage", "ready", "jsonl", "180", "11" * 16,
    ]
    if mode == "send":
        args.append(json.dumps(["body"]))
    else:
        args.extend(["body", json.dumps({"medre": {}})])
    monkeypatch.setattr(sys, "argv", args)
    exec(compile(_PEER_SCRIPT, "<lxmf-peer>", "exec"), {})
    result = json.loads(capsys.readouterr().out)["sent"][0]
    assert result["text"] == "body"
    assert result["state"] == Message.DELIVERED
    assert result["delivered"] is True and result["timed_out"] is False
