"""Focused tests for the cross-adapter diagnostics normalization contract.

Tests cover:

* ``dict`` input with common and adapter-specific fields.
* ``dataclass`` input.
* ``msgspec.Struct`` input (when msgspec is available).
* Plain-object (attribute) input.
* Missing fields resolve to ``None`` (not invented success).
* Nested ``transport_specific`` preserves adapter-specific data.
* Secret / unsafe key filtering.
* Deterministic JSON / msgspec-compatible serialization.
* No adapter imports – the helper is self-contained.
* ``adapter_hint`` / ``mode_hint`` overrides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from medre.core.runtime.diagnostic_contract import (
    COMMON_DIAGNOSTIC_KEYS,
    normalize_diagnostics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# msgspec is an optional dependency – skip msgspec tests when absent.
_msgspec = pytest.importorskip("msgspec", reason="msgspec not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeSessionDiagnostics:
    """Mimics a typical adapter session diagnostics dataclass."""

    connected: bool = True
    reconnecting: bool = False
    reconnect_attempts: int = 0
    last_error: str | None = None
    transient_delivery_failures: int = 0
    permanent_delivery_failures: int = 0
    node_id: str | None = "ABC123"
    channel_count: int = 8


class _MsgspecDiag(_msgspec.Struct, frozen=True):
    """Mimics a msgspec-struct diagnostics snapshot."""

    connected: bool = True
    reconnecting: bool = False
    reconnect_attempts: int = 3
    last_error: str | None = "timeout"
    transient_delivery_failures: int = 1
    permanent_delivery_failures: int = 0
    peer_count: int = 5
    custom_metric: float = 0.42


class _ObjectDiag:
    """Plain object with public attributes (no dict / dataclass)."""

    def __init__(self) -> None:
        self.connected = False
        self.reconnecting = True
        self.reconnect_attempts = 7
        self.last_error = "connection lost"
        self.transient_delivery_failures = 2
        self.permanent_delivery_failures = 1
        self.logged_in = True
        self.sync_task_running = True


# ===========================================================================
# 1. Dict input
# ===========================================================================


class TestDictInput:
    """Tests for raw ``dict`` diagnostics input."""

    def test_common_fields_extracted(self) -> None:
        raw = {
            "connected": True,
            "reconnecting": False,
            "reconnect_attempts": 0,
            "last_error": None,
            "transient_delivery_failures": 0,
            "permanent_delivery_failures": 0,
        }
        result = normalize_diagnostics(raw)

        assert result["connected"] is True
        assert result["reconnecting"] is False
        assert result["reconnect_attempts"] == 0
        assert result["last_error"] is None
        assert result["transient_delivery_failures"] == 0
        assert result["permanent_delivery_failures"] == 0
        assert result["health"] is None
        assert result["mode"] is None

    def test_missing_fields_are_none(self) -> None:
        """Missing common fields must be ``None``, not invented success."""
        raw = {"connected": True}
        result = normalize_diagnostics(raw)

        assert result["connected"] is True
        for key in COMMON_DIAGNOSTIC_KEYS - {"connected"}:
            assert result[key] is None, f"{key} should be None when missing"

    def test_extra_fields_go_to_transport_specific(self) -> None:
        raw = {
            "connected": True,
            "node_id": "N123",
            "channel_count": 3,
            "custom_status": "ok",
        }
        result = normalize_diagnostics(raw)

        assert "transport_specific" in result
        assert result["transport_specific"]["node_id"] == "N123"
        assert result["transport_specific"]["channel_count"] == 3
        assert result["transport_specific"]["custom_status"] == "ok"

    def test_no_transport_specific_when_empty(self) -> None:
        raw = {"connected": False}
        result = normalize_diagnostics(raw)

        # If only common keys are present, transport_specific should be absent.
        assert "transport_specific" not in result

    def test_empty_dict_produces_all_none_common(self) -> None:
        result = normalize_diagnostics({})

        for key in COMMON_DIAGNOSTIC_KEYS:
            assert result[key] is None


# ===========================================================================
# 2. Dataclass input
# ===========================================================================


class TestDataclassInput:
    """Tests for dataclass diagnostics input."""

    def test_dataclass_fields_extracted(self) -> None:
        diag = FakeSessionDiagnostics()
        result = normalize_diagnostics(diag)

        assert result["connected"] is True
        assert result["reconnecting"] is False
        assert result["reconnect_attempts"] == 0
        assert result["last_error"] is None
        assert result["transient_delivery_failures"] == 0
        assert result["permanent_delivery_failures"] == 0

    def test_dataclass_specific_fields_preserved(self) -> None:
        diag = FakeSessionDiagnostics(node_id="NODE_X", channel_count=4)
        result = normalize_diagnostics(diag)

        assert result["transport_specific"]["node_id"] == "NODE_X"
        assert result["transport_specific"]["channel_count"] == 4

    def test_dataclass_health_and_mode_default_none(self) -> None:
        diag = FakeSessionDiagnostics()
        result = normalize_diagnostics(diag)

        assert result["health"] is None
        assert result["mode"] is None


# ===========================================================================
# 3. msgspec Struct input
# ===========================================================================


class TestMsgspecInput:
    """Tests for ``msgspec.Struct`` diagnostics input."""

    def test_struct_fields_extracted(self) -> None:
        diag = _MsgspecDiag()
        result = normalize_diagnostics(diag)

        assert result["connected"] is True
        assert result["reconnecting"] is False
        assert result["reconnect_attempts"] == 3
        assert result["last_error"] == "timeout"
        assert result["transient_delivery_failures"] == 1
        assert result["permanent_delivery_failures"] == 0

    def test_struct_specific_preserved(self) -> None:
        diag = _MsgspecDiag()
        result = normalize_diagnostics(diag)

        assert result["transport_specific"]["peer_count"] == 5
        assert result["transport_specific"]["custom_metric"] == pytest.approx(0.42)

    def test_struct_frozen_works(self) -> None:
        """Frozen structs should still be readable via getattr."""
        diag = _MsgspecDiag(connected=False, reconnecting=True)
        result = normalize_diagnostics(diag)

        assert result["connected"] is False
        assert result["reconnecting"] is True


# ===========================================================================
# 4. Plain object input
# ===========================================================================


class TestObjectInput:
    """Tests for plain-object (attribute) diagnostics input."""

    def test_object_attributes_extracted(self) -> None:
        diag = _ObjectDiag()
        result = normalize_diagnostics(diag)

        assert result["connected"] is False
        assert result["reconnecting"] is True
        assert result["reconnect_attempts"] == 7
        assert result["last_error"] == "connection lost"
        assert result["transient_delivery_failures"] == 2
        assert result["permanent_delivery_failures"] == 1

    def test_object_specific_fields_preserved(self) -> None:
        diag = _ObjectDiag()
        result = normalize_diagnostics(diag)

        assert result["transport_specific"]["logged_in"] is True
        assert result["transport_specific"]["sync_task_running"] is True


# ===========================================================================
# 5. Secret / unsafe filtering
# ===========================================================================


class TestSecretFiltering:
    """Tests that secret/private keys are filtered from output."""

    @pytest.mark.parametrize(
        "secret_key",
        [
            "password",
            "secret",
            "secret_key",
            "private_key",
            "privatekey",
            "access_token",
            "auth_token",
            "api_key",
            "credentials",
            "session_secret",
            "encryption_key",
        ],
    )
    def test_secret_keys_dropped_from_common(self, secret_key: str) -> None:
        """Secret keys must not appear in common output."""
        raw = {
            "connected": True,
            secret_key: "should_be_removed",
        }
        result = normalize_diagnostics(raw)

        # The secret key should not appear in the common section.
        # (It could be a common key name like nothing, so check it's not
        # in transport_specific either.)
        for section_key in result:
            if isinstance(result[section_key], dict):
                assert secret_key not in result[section_key]
            else:
                assert section_key != secret_key

    @pytest.mark.parametrize(
        "secret_key",
        [
            "password",
            "access_token",
            "private_key",
            "api_key",
            "credentials",
        ],
    )
    def test_secret_keys_dropped_from_transport_specific(
        self, secret_key: str
    ) -> None:
        """Secret keys in adapter-specific fields must be dropped."""
        raw = {
            "connected": True,
            "custom_field": "safe_value",
            secret_key: "super_secret_123",
        }
        result = normalize_diagnostics(raw)

        assert "transport_specific" in result
        assert secret_key not in result["transport_specific"]
        assert result["transport_specific"]["custom_field"] == "safe_value"

    def test_nested_dict_secret_filtering(self) -> None:
        """Secret keys in nested dicts should be filtered."""
        raw = {
            "connected": True,
            "nested_info": {
                "safe_key": "ok",
                "password": "hidden",
                "access_token": "also_hidden",
            },
        }
        result = normalize_diagnostics(raw)

        nested = result["transport_specific"]["nested_info"]
        assert nested["safe_key"] == "ok"
        assert "password" not in nested
        assert "access_token" not in nested


# ===========================================================================
# 6. Unsafe value sanitization
# ===========================================================================


class TestUnsafeValueSanitization:
    """Tests that complex/unsafe values are converted to safe forms."""

    def test_exception_becomes_type_placeholder(self) -> None:
        """Exceptions become '<ValueError>', not full repr with message."""
        raw = {
            "connected": True,
            "last_error": ValueError("boom"),
        }
        result = normalize_diagnostics(raw)

        assert isinstance(result["last_error"], str)
        assert result["last_error"] == "<ValueError>"
        # The error message must NOT leak through.
        assert "boom" not in result["last_error"]

    def test_raw_object_becomes_type_placeholder(self) -> None:
        """Custom objects become '<ClassName>', not full repr."""

        class SDKClient:
            def __repr__(self) -> str:
                return "<SDKClient secret=abc123>"

        raw = {
            "connected": True,
            "client": SDKClient(),
        }
        result = normalize_diagnostics(raw)

        val = result["transport_specific"]["client"]
        assert isinstance(val, str)
        assert val == "<SDKClient>"
        # repr content must not leak.
        assert "secret" not in val
        assert "abc123" not in val

    def test_function_becomes_type_placeholder(self) -> None:
        raw = {
            "connected": True,
            "callback": lambda: None,
        }
        result = normalize_diagnostics(raw)

        val = result["transport_specific"]["callback"]
        assert isinstance(val, str)
        assert val == "<function>"

    def test_bytes_becomes_type_placeholder(self) -> None:
        """bytes become '<bytes>', not b'...' repr."""
        raw = {
            "connected": True,
            "raw_payload": b"\x00\x01secret",
        }
        result = normalize_diagnostics(raw)

        val = result["transport_specific"]["raw_payload"]
        assert val == "<bytes>"
        assert "secret" not in val

    def test_list_values_sanitized(self) -> None:
        raw = {
            "connected": True,
            "errors": [ValueError("a"), "plain_string"],
        }
        result = normalize_diagnostics(raw)

        errors = result["transport_specific"]["errors"]
        assert isinstance(errors, list)
        assert errors[0] == "<ValueError>"
        assert errors[1] == "plain_string"

    def test_secret_in_repr_not_leaked(self) -> None:
        """A custom object whose repr includes token-like strings must not
        leak those strings into the diagnostic output."""

        class TokenHolder:
            def __repr__(self) -> str:
                return "TokenHolder(token='sk_live_abc123xyz')"

        raw = {"connected": True, "holder": TokenHolder()}
        result = normalize_diagnostics(raw)

        val = result["transport_specific"]["holder"]
        assert val == "<TokenHolder>"
        assert "sk_live" not in val
        assert "abc123" not in val

    def test_exception_subclass_name(self) -> None:
        """Subclassed exceptions use the subclass name."""

        class CustomTimeout(TimeoutError):
            pass

        raw = {"connected": True, "last_error": CustomTimeout("timed out")}
        result = normalize_diagnostics(raw)

        assert result["last_error"] == "<CustomTimeout>"
        assert "timed out" not in result["last_error"]

    def test_set_becomes_sanitized_list(self) -> None:
        """Sets are recursively sanitized into lists."""
        raw = {"connected": True, "tags": {ValueError("x"), 42}}
        result = normalize_diagnostics(raw)

        tags = result["transport_specific"]["tags"]
        assert isinstance(tags, list)
        assert 42 in tags
        assert "<ValueError>" in tags

    def test_dict_values_with_unknown_objects(self) -> None:
        """Nested dicts with unknown objects sanitize correctly."""

        class Conn:
            def __repr__(self) -> str:
                return "Conn(session_key=deadbeef)"

        raw = {
            "connected": True,
            "meta": {"conn": Conn(), "count": 3},
        }
        result = normalize_diagnostics(raw)

        meta = result["transport_specific"]["meta"]
        assert meta["conn"] == "<Conn>"
        assert meta["count"] == 3
        assert "deadbeef" not in meta["conn"]

    def test_string_scalar_passes_through(self) -> None:
        """Plain strings (even with token-like content) pass through as-is,
        because they are scalar values the caller intentionally provided."""
        raw = {"connected": True, "note": "sk_live_abc123 is my token"}
        result = normalize_diagnostics(raw)

        assert result["transport_specific"]["note"] == "sk_live_abc123 is my token"

    def test_none_preserved(self) -> None:
        raw = {"connected": None, "last_error": None}
        result = normalize_diagnostics(raw)

        assert result["connected"] is None
        assert result["last_error"] is None

    def test_bool_int_float_str_preserved(self) -> None:
        raw = {
            "connected": True,
            "reconnect_attempts": 5,
            "ratio": 0.75,
            "mode": "live",
        }
        result = normalize_diagnostics(raw)

        assert result["connected"] is True
        assert result["reconnect_attempts"] == 5
        assert result["mode"] == "live"
        # ratio is a transport-specific field
        assert result["transport_specific"]["ratio"] == 0.75


# ===========================================================================
# 7. Deterministic serialization
# ===========================================================================


class TestDeterministicSerialization:
    """Tests that output is deterministic and JSON/msgspec-serializable."""

    def test_stable_key_order(self) -> None:
        """Repeated calls produce identical key order."""
        raw = {
            "connected": True,
            "reconnecting": False,
            "node_id": "X",
            "channel_count": 2,
        }
        r1 = normalize_diagnostics(raw)
        r2 = normalize_diagnostics(raw)

        assert list(r1.keys()) == list(r2.keys())

    def test_json_serializable(self) -> None:
        """The full output dict must be JSON-serializable."""
        raw = {
            "connected": True,
            "reconnecting": False,
            "reconnect_attempts": 2,
            "last_error": "timeout",
            "transient_delivery_failures": 1,
            "permanent_delivery_failures": 0,
            "health": "degraded",
            "mode": "live",
            "node_id": "N1",
        }
        result = normalize_diagnostics(raw)

        # Must not raise.
        serialized = json.dumps(result, sort_keys=True)
        assert isinstance(serialized, str)

        # Round-trip must preserve values.
        round_tripped = json.loads(serialized)
        assert round_tripped["connected"] is True
        assert round_tripped["health"] == "degraded"

    def test_msgspec_encodable(self) -> None:
        """The output must be encodable by msgspec."""
        raw = {
            "connected": True,
            "custom_field": 42,
        }
        result = normalize_diagnostics(raw)

        # Must not raise.
        encoded = _msgspec.json.encode(result)
        assert isinstance(encoded, bytes)

        decoded = _msgspec.json.decode(encoded)
        assert decoded["connected"] is True
        assert decoded["transport_specific"]["custom_field"] == 42

    def test_transport_specific_sorted_keys(self) -> None:
        """Transport-specific keys must be in sorted order."""
        raw = {
            "connected": True,
            "z_field": 1,
            "a_field": 2,
            "m_field": 3,
        }
        result = normalize_diagnostics(raw)

        specific_keys = list(result["transport_specific"].keys())
        assert specific_keys == sorted(specific_keys)


# ===========================================================================
# 8. Adapter hint / mode hint
# ===========================================================================


class TestHints:
    """Tests for adapter_hint and mode_hint parameters."""

    def test_adapter_hint_included(self) -> None:
        result = normalize_diagnostics(
            {"connected": True}, adapter_hint="meshtastic"
        )

        assert result["adapter"] == "meshtastic"

    def test_adapter_hint_none_omitted(self) -> None:
        result = normalize_diagnostics({"connected": True})

        assert "adapter" not in result

    def test_mode_hint_overrides_raw(self) -> None:
        raw = {"connected": True, "mode": "live"}
        result = normalize_diagnostics(raw, mode_hint="fake")

        assert result["mode"] == "fake"

    def test_mode_hint_when_missing(self) -> None:
        raw = {"connected": True}
        result = normalize_diagnostics(raw, mode_hint="live")

        assert result["mode"] == "live"


# ===========================================================================
# 9. No adapter imports
# ===========================================================================


class TestNoAdapterImports:
    """Verify the diagnostic_contract module does not import adapters."""

    def test_module_imports_no_adapters(self) -> None:
        import medre.core.runtime.diagnostic_contract as mod
        import sys

        # Collect all loaded modules that start with medre.adapters.
        adapter_modules = [
            name
            for name in sys.modules
            if name.startswith("medre.adapters")
        ]

        # The diagnostic_contract module itself should not have caused any
        # adapter modules to be imported.  (They might be imported by other
        # test fixtures, but we check the module's own __dict__ refs.)
        mod_source = open(mod.__file__).read() if mod.__file__ else ""
        assert "medre.adapters" not in mod_source
        assert "from medre.adapters" not in mod_source
        assert "import medre.adapters" not in mod_source


# ===========================================================================
# 10. Observational-only semantics
# ===========================================================================


class TestObservationalOnly:
    """Tests that the helper never infers authoritative state."""

    def test_no_invented_connected(self) -> None:
        """Empty input must not claim connected=True."""
        result = normalize_diagnostics({})
        assert result["connected"] is None

    def test_no_invented_health(self) -> None:
        """Empty input must not invent 'healthy' status."""
        result = normalize_diagnostics({})
        assert result["health"] is None

    def test_no_invented_mode(self) -> None:
        """Mode must not be guessed when absent."""
        result = normalize_diagnostics({})
        assert result["mode"] is None

    def test_no_zero_reconnect_when_missing(self) -> None:
        """Missing reconnect_attempts must be None, not 0."""
        result = normalize_diagnostics({})
        assert result["reconnect_attempts"] is None

    def test_no_zero_failures_when_missing(self) -> None:
        """Missing failure counts must be None, not 0."""
        result = normalize_diagnostics({})
        assert result["transient_delivery_failures"] is None
        assert result["permanent_delivery_failures"] is None

    def test_explicit_false_connected_preserved(self) -> None:
        """Explicit connected=False must not become None."""
        result = normalize_diagnostics({"connected": False})
        assert result["connected"] is False

    def test_explicit_zero_preserved(self) -> None:
        """Explicit reconnect_attempts=0 must not become None."""
        result = normalize_diagnostics({"reconnect_attempts": 0})
        assert result["reconnect_attempts"] == 0


# ===========================================================================
# 11. Cross-shape equivalence
# ===========================================================================


class TestCrossShapeEquivalence:
    """Verify different input shapes produce equivalent output."""

    def test_dict_vs_dataclass_same_output(self) -> None:
        diag = FakeSessionDiagnostics()
        from_dict = normalize_diagnostics(
            {
                "connected": diag.connected,
                "reconnecting": diag.reconnecting,
                "reconnect_attempts": diag.reconnect_attempts,
                "last_error": diag.last_error,
                "transient_delivery_failures": diag.transient_delivery_failures,
                "permanent_delivery_failures": diag.permanent_delivery_failures,
                "node_id": diag.node_id,
                "channel_count": diag.channel_count,
            }
        )
        from_dc = normalize_diagnostics(diag)

        # Common fields should match.
        for key in COMMON_DIAGNOSTIC_KEYS:
            assert from_dict[key] == from_dc[key], f"Mismatch on {key}"

        # Transport-specific should match too.
        assert (
            from_dict["transport_specific"]["node_id"]
            == from_dc["transport_specific"]["node_id"]
        )
        assert (
            from_dict["transport_specific"]["channel_count"]
            == from_dc["transport_specific"]["channel_count"]
        )

    def test_dict_vs_msgspec_same_output(self) -> None:
        struct = _MsgspecDiag()
        from_dict = normalize_diagnostics(
            {
                "connected": struct.connected,
                "reconnecting": struct.reconnecting,
                "reconnect_attempts": struct.reconnect_attempts,
                "last_error": struct.last_error,
                "transient_delivery_failures": struct.transient_delivery_failures,
                "permanent_delivery_failures": struct.permanent_delivery_failures,
                "peer_count": struct.peer_count,
                "custom_metric": struct.custom_metric,
            }
        )
        from_struct = normalize_diagnostics(struct)

        for key in COMMON_DIAGNOSTIC_KEYS:
            assert from_dict[key] == from_struct[key], f"Mismatch on {key}"


# ===========================================================================
# 12. Known limitation: nested session diagnostics
# ===========================================================================


class TestNestedSessionDiagnosticsLimitation:
    """Track 5: document that normalize_diagnostics operates on flat keys only.

    Some adapters (Meshtastic, MeshCore, LXMF) nest delivery failure counters
    under a ``session`` sub-dict rather than at the top level.  The
    ``normalize_diagnostics`` contract extracts common keys from the **top
    level** of the input dict only — it does not recurse into nested dicts.

    This is an intentional design choice: the contract is observational and
    does not invent cross-transport normalization.  Consumers that need
    delivery failure counters from these adapters must access
    ``session.transient_delivery_failures`` directly from the raw adapter
    diagnostics, not via the normalized output.
    """

    def test_nested_transient_failures_not_extracted(self) -> None:
        """Delivery counters nested under 'session' resolve to None."""
        raw = {
            "adapter_id": "mesh-1",
            "started": True,
            "session": {
                "connected": True,
                "reconnecting": False,
                "reconnect_attempts": 0,
                "transient_delivery_failures": 5,
                "permanent_delivery_failures": 1,
                "last_error": None,
            },
        }
        result = normalize_diagnostics(raw)

        # Top-level extraction finds connected via the session sub-dict?  No.
        # The contract only looks at the top-level keys of *raw*.
        assert result["transient_delivery_failures"] is None
        assert result["permanent_delivery_failures"] is None

        # The nested session dict is preserved in transport_specific.
        assert result["transport_specific"]["session"]["transient_delivery_failures"] == 5
        assert result["transport_specific"]["session"]["permanent_delivery_failures"] == 1

    def test_flat_transient_failures_extracted(self) -> None:
        """Delivery counters at the top level are extracted correctly."""
        raw = {
            "connected": True,
            "reconnecting": False,
            "reconnect_attempts": 0,
            "transient_delivery_failures": 3,
            "permanent_delivery_failures": 0,
            "last_error": None,
        }
        result = normalize_diagnostics(raw)
        assert result["transient_delivery_failures"] == 3
        assert result["permanent_delivery_failures"] == 0
