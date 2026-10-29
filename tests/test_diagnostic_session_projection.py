"""Nested provider observations retain common diagnostic values and precedence."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

import msgspec
import pytest

from medre.core.supervision.diagnostic_contract import (
    COMMON_DIAGNOSTIC_KEYS,
    normalize_diagnostics,
)


@dataclass
class SessionSnapshot:
    connected: bool
    reconnecting: bool
    reconnect_attempts: int
    transient_delivery_failures: int
    permanent_delivery_failures: int
    last_error: str


class StructuredSnapshot(msgspec.Struct):
    connected: bool
    reconnecting: bool
    reconnect_attempts: int
    transient_delivery_failures: int
    permanent_delivery_failures: int
    last_error: str


@pytest.mark.parametrize("session_shape", ["mapping", "dataclass", "struct", "object"])
def test_nested_session_observations_reach_common_projection(session_shape):
    """Known native observations survive a provider's nested session snapshot."""
    session = {
        "connected": True,
        "reconnecting": True,
        "reconnect_attempts": 3,
        "transient_delivery_failures": 7,
        "permanent_delivery_failures": 2,
        "last_error": "endpoint unavailable",
    }
    if session_shape == "dataclass":
        session = SessionSnapshot(**session)
    elif session_shape == "struct":
        session = StructuredSnapshot(**session)
    elif session_shape == "object":
        session = SimpleNamespace(**session)
    raw = {"health": "degraded", "mode": "live", "session": session}
    projected = normalize_diagnostics(raw)
    assert projected["connected"] is True
    assert projected["reconnecting"] is True
    assert projected["reconnect_attempts"] == 3
    assert projected["transient_delivery_failures"] == 7
    assert projected["permanent_delivery_failures"] == 2
    assert projected["last_error"] == "endpoint unavailable"
    assert projected["health"] == "degraded"
    assert raw["session"] is session


@pytest.mark.parametrize(
    ("key", "value", "nested"),
    [
        ("connected", None, True),
        ("connected", False, True),
        ("reconnect_attempts", 0, 3),
        ("last_error", "", "endpoint unavailable"),
    ],
)
def test_explicit_outer_observations_override_nested_session(key, value, nested):
    """Explicit unknowns, false values and zero counts retain provider authority."""
    result = normalize_diagnostics({key: value, "session": {key: nested}})
    assert result[key] == value


def test_nested_session_projection_retains_details_and_filters_secrets():
    """Promoting common keys cannot remove session detail or expose unsafe values."""
    from copy import deepcopy

    raw = {
        "session": {
            "connected": True,
            "peer_count": 4,
            "access_token": "private",
            "last_error": ValueError("private"),
        }
    }
    original = deepcopy(raw)
    result = normalize_diagnostics(raw)
    assert result["connected"] is True
    assert result["last_error"] == "<ValueError>"
    assert result["transport_specific"]["session"]["peer_count"] == 4
    assert "access_token" not in result["transport_specific"]["session"]
    assert "private" not in json.dumps(result)
    assert raw["session"]["access_token"] == original["session"]["access_token"]


@pytest.mark.parametrize("session", [None, 1, "unknown", ["connected"], True])
def test_unstructured_session_is_not_promoted(session):
    """Opaque native session placeholders cannot establish common observations."""
    result = normalize_diagnostics({"session": session})
    assert all(result[key] is None for key in COMMON_DIAGNOSTIC_KEYS)
