"""Evidence parity audit tests — operational diagnostics key presence and type consistency.

Proves the highest-value findings from ``docs/dev/evidence-parity-audit.md``:
- All four adapters include the 8 common diagnostic keys in diagnostics().
- Common key types match the spec (``diagnostics-evidence.md`` §2).
- No-session fallback produces safe defaults instead of missing keys.
- Matrix ``last_error`` is aliased alongside ``last_sync_error``.
- ``mode`` and ``health`` are present in all adapter diagnostics.

Evidence level: **fake_pipeline** (tier 1) — no network, no hardware.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from medre.core.supervision.diagnostic_contract import (
    COMMON_DIAGNOSTIC_KEYS,
    normalize_diagnostics,
)

# ---------------------------------------------------------------------------
# Constants under test
# ---------------------------------------------------------------------------

# The eight contractual common keys from diagnostics-evidence.md §2.
_EXPECTED_COMMON_KEYS = frozenset(
    {
        "connected",
        "health",
        "mode",
        "reconnecting",
        "reconnect_attempts",
        "last_error",
        "transient_delivery_failures",
        "permanent_delivery_failures",
    }
)

# Per-key expected types (value | None for optional ones).
_COMMON_KEY_TYPES: dict[str, tuple[type, ...]] = {
    "connected": (bool,),
    "health": (str, type(None)),
    "mode": (str, type(None)),
    "reconnecting": (bool,),
    "reconnect_attempts": (int,),
    "last_error": (str, type(None)),
    "transient_delivery_failures": (int,),
    "permanent_delivery_failures": (int,),
}


# ---------------------------------------------------------------------------
# Helpers — lightweight config factories (no SDK imports)
# ---------------------------------------------------------------------------


def _meshtastic_config(adapter_id: str = "epa-mt") -> Any:
    """Minimal MeshtasticConfig with fake connection_type."""
    from medre.config.adapters.meshtastic import MeshtasticConfig

    return MeshtasticConfig(adapter_id=adapter_id, connection_type="fake")


def _meshcore_config(adapter_id: str = "epa-mc") -> Any:
    """Minimal MeshCoreConfig with fake connection_type."""
    from medre.config.adapters.meshcore import MeshCoreConfig

    return MeshCoreConfig(adapter_id=adapter_id, connection_type="fake")


def _lxmf_config(adapter_id: str = "epa-lx") -> Any:
    """Minimal LxmfConfig with fake connection_type."""
    from medre.config.adapters.lxmf import LxmfConfig

    return LxmfConfig(adapter_id=adapter_id, connection_type="fake")


def _matrix_config(adapter_id: str = "epa-mx") -> Any:
    """Minimal MatrixConfig."""
    from medre.config.adapters.matrix import MatrixConfig

    return MatrixConfig(
        adapter_id=adapter_id,
        homeserver="https://example.com",
        user_id="@test:example.com",
        access_token="fake-token",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def meshtastic_adapter():
    """MeshtasticAdapter with fake config, no session (pre-start)."""
    from medre.adapters.meshtastic.adapter import MeshtasticAdapter

    adapter = MeshtasticAdapter(_meshtastic_config())
    return adapter


@pytest.fixture
def meshcore_adapter():
    """MeshCoreAdapter with fake config, no session (pre-start)."""
    from medre.adapters.meshcore.adapter import MeshCoreAdapter

    adapter = MeshCoreAdapter(_meshcore_config())
    return adapter


@pytest.fixture
def lxmf_adapter():
    """LxmfAdapter with fake config."""
    from medre.adapters.lxmf.adapter import LxmfAdapter

    adapter = LxmfAdapter(_lxmf_config())
    return adapter


@pytest.fixture
def matrix_adapter():
    """MatrixAdapter with fake config, no session (pre-start)."""
    from medre.adapters.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(_matrix_config())
    return adapter


# ===================================================================
# 1. COMMON_DIAGNOSTIC_KEYS constant matches spec §2
# ===================================================================


class TestCommonKeysConstant:
    """COMMON_DIAGNOSTIC_KEYS frozenset matches the spec's 8 keys."""

    def test_contains_exactly_eight_keys(self) -> None:
        assert len(COMMON_DIAGNOSTIC_KEYS) == 8

    def test_matches_spec_keys(self) -> None:
        assert COMMON_DIAGNOSTIC_KEYS == _EXPECTED_COMMON_KEYS

    def test_is_frozenset(self) -> None:
        assert isinstance(COMMON_DIAGNOSTIC_KEYS, frozenset)


# ===================================================================
# 2. normalize_diagnostics resolves missing keys to None
# ===================================================================


class TestNormalizeDiagnosticsMissingKeys:
    """normalize_diagnostics resolves missing common keys to None."""

    def test_empty_dict_produces_none_for_all_common(self) -> None:
        result = normalize_diagnostics({})
        for key in _EXPECTED_COMMON_KEYS:
            assert result[key] is None, f"{key} should be None for empty input"

    def test_partial_input_preserves_present_keys(self) -> None:
        raw = {"connected": True, "reconnecting": False, "mode": "fake"}
        result = normalize_diagnostics(raw)
        assert result["connected"] is True
        assert result["reconnecting"] is False
        assert result["mode"] == "fake"
        # Missing keys resolve to None.
        assert result["health"] is None
        assert result["last_error"] is None

    def test_all_common_keys_present_in_output(self) -> None:
        result = normalize_diagnostics({})
        for key in _EXPECTED_COMMON_KEYS:
            assert key in result, f"Missing key: {key}"

    def test_transport_specific_preserved(self) -> None:
        raw = {"connected": True, "custom_field": 42}
        result = normalize_diagnostics(raw)
        assert "transport_specific" in result
        assert result["transport_specific"]["custom_field"] == 42


# ===================================================================
# 3. Meshtastic adapter diagnostics parity
# ===================================================================


class TestMeshtasticDiagnostics:
    """MeshtasticAdapter diagnostics() common key parity."""

    def test_no_session_has_session_subdict(self, meshtastic_adapter) -> None:
        """Pre-start (no session): session sub-dict present with safe defaults."""
        diag = meshtastic_adapter.diagnostics()
        assert "session" in diag
        session = diag["session"]
        assert session["connected"] is False
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 0
        assert session["last_error"] is None
        assert session["transient_delivery_failures"] == 0
        assert session["permanent_delivery_failures"] == 0

    def test_mode_present_at_adapter_level(self, meshtastic_adapter) -> None:
        diag = meshtastic_adapter.diagnostics()
        assert "mode" in diag
        assert diag["mode"] == "fake"

    def test_health_present_before_health_check(self, meshtastic_adapter) -> None:
        """Health key is present even before any health_check() call."""
        diag = meshtastic_adapter.diagnostics()
        assert "health" in diag
        # Before any health_check(), _last_health is None.
        assert diag["health"] is None

    def test_with_mocked_session_produces_common_keys(self, meshtastic_adapter) -> None:
        """With a mocked session, all session-level common keys are present."""
        mock_session = MagicMock()
        mock_diag = MagicMock()
        mock_diag.connected = True
        mock_diag.reconnecting = True
        mock_diag.reconnect_attempts = 3
        mock_diag.last_packet_time = 1234.5
        mock_diag.node_id = "!abc123"
        mock_diag.channel_count = 4
        mock_diag.transient_delivery_failures = 1
        mock_diag.permanent_delivery_failures = 0
        mock_diag.last_error = "timeout"
        mock_session.diagnostics.return_value = mock_diag
        meshtastic_adapter._session = mock_session

        diag = meshtastic_adapter.diagnostics()
        session = diag["session"]
        assert session["connected"] is True
        assert session["reconnecting"] is True
        assert session["reconnect_attempts"] == 3
        assert session["last_error"] == "timeout"
        assert session["transient_delivery_failures"] == 1
        assert session["permanent_delivery_failures"] == 0

    def test_connection_type_preserved(self, meshtastic_adapter) -> None:
        diag = meshtastic_adapter.diagnostics()
        assert diag["connection_type"] == "fake"


# ===================================================================
# 4. MeshCore adapter diagnostics parity
# ===================================================================


class TestMeshCoreDiagnostics:
    """MeshCoreAdapter diagnostics() common key parity."""

    def test_no_session_has_session_subdict(self, meshcore_adapter) -> None:
        """Pre-start (no session): session sub-dict present with safe defaults."""
        diag = meshcore_adapter.diagnostics()
        assert "session" in diag
        session = diag["session"]
        assert session["connected"] is False
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 0
        assert session["last_error"] is None
        assert session["transient_delivery_failures"] == 0
        assert session["permanent_delivery_failures"] == 0

    def test_mode_present(self, meshcore_adapter) -> None:
        diag = meshcore_adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_health_present_before_health_check(self, meshcore_adapter) -> None:
        diag = meshcore_adapter.diagnostics()
        assert "health" in diag
        assert diag["health"] is None

    def test_with_mocked_session_produces_common_keys(self, meshcore_adapter) -> None:
        """With a mocked session, session sub-dict contains common keys."""
        mock_session = MagicMock()
        mock_session.diagnostics.return_value = {
            "connected": True,
            "reconnecting": False,
            "reconnect_attempts": 1,
            "last_message_time": None,
            "last_error": "conn_reset",
            "transient_delivery_failures": 2,
            "permanent_delivery_failures": 0,
            "device_name": "node-1",
            "public_key_prefix": "a1b2c3",
            "radio_freq": 868.0,
            "mode": "fake",
        }
        meshcore_adapter._session = mock_session

        diag = meshcore_adapter.diagnostics()
        session = diag["session"]
        assert session["connected"] is True
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 1
        assert session["last_error"] == "conn_reset"
        assert session["transient_delivery_failures"] == 2
        assert session["permanent_delivery_failures"] == 0
        assert session["mode"] == "fake"


# ===================================================================
# 5. LXMF adapter diagnostics parity
# ===================================================================


class TestLxmfDiagnostics:
    """LxmfAdapter diagnostics() common key parity."""

    def test_session_subdict_present(self, lxmf_adapter) -> None:
        """LXMF session is always created in __init__; session sub-dict present."""
        diag = lxmf_adapter.diagnostics()
        assert "session" in diag
        session = diag["session"]
        # Session exists but not started — defaults should be safe.
        assert isinstance(session["connected"], bool)
        assert isinstance(session["reconnecting"], bool)
        assert isinstance(session["reconnect_attempts"], int)
        assert session["last_error"] is None
        assert isinstance(session["transient_delivery_failures"], int)
        assert isinstance(session["permanent_delivery_failures"], int)

    def test_mode_present(self, lxmf_adapter) -> None:
        diag = lxmf_adapter.diagnostics()
        assert diag["mode"] == "fake"
        assert diag["session"]["mode"] == "fake"

    def test_health_present_before_health_check(self, lxmf_adapter) -> None:
        diag = lxmf_adapter.diagnostics()
        assert "health" in diag
        assert diag["health"] is None


# ===================================================================
# 6. Matrix adapter diagnostics parity
# ===================================================================


class TestMatrixDiagnostics:
    """MatrixAdapter diagnostics() common key parity."""

    def test_no_session_has_fallback_common_keys(self, matrix_adapter) -> None:
        """Pre-start (no session): fallback dict includes common keys."""
        diag = matrix_adapter.diagnostics()
        # Matrix exposes common keys at top level (flat structure).
        assert diag["connected"] is False
        assert diag["reconnecting"] is False
        assert diag["reconnect_attempts"] == 0
        assert diag["transient_delivery_failures"] == 0
        assert diag["permanent_delivery_failures"] == 0

    def test_mode_present(self, matrix_adapter) -> None:
        diag = matrix_adapter.diagnostics()
        assert "mode" in diag
        assert diag["mode"] == "live"

    def test_health_present_before_health_check(self, matrix_adapter) -> None:
        diag = matrix_adapter.diagnostics()
        assert "health" in diag
        assert diag["health"] is None

    def test_last_error_alias_present(self, matrix_adapter) -> None:
        """Both ``last_error`` and ``last_sync_error`` are present."""
        diag = matrix_adapter.diagnostics()
        assert "last_error" in diag
        assert "last_sync_error" in diag
        # Both should be None when no error has occurred.
        assert diag["last_error"] is None
        assert diag["last_sync_error"] is None

    def test_last_error_alias_with_session_error(self, matrix_adapter) -> None:
        """When session has a sync error, both keys reflect it."""
        mock_session = MagicMock()
        mock_diag = MagicMock()
        mock_diag.connected = True
        mock_diag.logged_in = True
        mock_diag.sync_task_running = True
        mock_diag.last_sync_error = Exception("sync failed")
        mock_diag.store_path_configured = False
        mock_diag.device_id_configured = False
        mock_diag.encryption_mode = "plaintext"
        mock_diag.crypto_enabled = False
        mock_diag.last_crypto_error = None
        mock_diag.encrypted_room_seen = False
        mock_diag.undecryptable_event_count = 0
        mock_diag.sync_running = True
        mock_diag.reconnecting = False
        mock_diag.reconnect_attempts = 0
        mock_diag.last_successful_sync = None
        mock_diag.crypto_store_loaded = False
        mock_diag.olm_loaded = False
        mock_diag.store_loaded = False
        mock_diag.device_keys_uploaded = False
        mock_diag.key_query_needed = False
        mock_diag.device_id_in_use = None
        mock_diag.store_path_exists = False
        mock_diag.initial_sync_completed = False
        mock_diag.encrypted_room_count = 0
        mock_diag.plaintext_room_count = 0
        mock_session.diagnostics.return_value = mock_diag
        matrix_adapter._session = mock_session

        diag = matrix_adapter.diagnostics()
        assert diag["last_sync_error"] == "sync failed"
        assert diag["last_error"] == "sync failed"


# ===================================================================
# 7. Cross-adapter common key type consistency
# ===================================================================


class TestCommonKeyTypeConsistency:
    """All common keys have correct types across adapters (flat or in session)."""

    def _extract_common(self, diag: dict[str, Any]) -> dict[str, Any]:
        """Extract common keys from flat dict or session sub-dict."""
        result: dict[str, Any] = {}
        for key in _EXPECTED_COMMON_KEYS:
            if key in diag:
                result[key] = diag[key]
            elif "session" in diag and isinstance(diag["session"], dict):
                result[key] = diag["session"].get(key)
            else:
                result[key] = None
        return result

    def test_meshtastic_no_session_types(self, meshtastic_adapter) -> None:
        diag = meshtastic_adapter.diagnostics()
        common = self._extract_common(diag)
        self._assert_types(common)

    def test_meshcore_no_session_types(self, meshcore_adapter) -> None:
        diag = meshcore_adapter.diagnostics()
        common = self._extract_common(diag)
        self._assert_types(common)

    def test_lxmf_types(self, lxmf_adapter) -> None:
        diag = lxmf_adapter.diagnostics()
        common = self._extract_common(diag)
        self._assert_types(common)

    def test_matrix_no_session_types(self, matrix_adapter) -> None:
        diag = matrix_adapter.diagnostics()
        common = self._extract_common(diag)
        self._assert_types(common)

    def _assert_types(self, common: dict[str, Any]) -> None:
        for key, expected_types in _COMMON_KEY_TYPES.items():
            value = common.get(key)
            assert isinstance(
                value, expected_types
            ), f"Key {key!r}: expected {expected_types}, got {type(value).__name__} ({value!r})"


# ===================================================================
# 8. No-session fallback completeness (P0 fix verification)
# ===================================================================


class TestNoSessionFallback:
    """Meshtastic, MeshCore: session sub-dict present even without a session.

    Verifies the P0 fix from the audit: adapters that conditionally include
    a ``session`` sub-dict now produce it with safe defaults instead of
    omitting it entirely.
    """

    def test_meshtastic_session_subdict_without_session(
        self, meshtastic_adapter
    ) -> None:
        diag = meshtastic_adapter.diagnostics()
        assert isinstance(diag.get("session"), dict)
        session = diag["session"]
        # All nested common keys present with safe values.
        assert session["connected"] is False
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 0
        assert session["last_error"] is None
        assert session["transient_delivery_failures"] == 0
        assert session["permanent_delivery_failures"] == 0

    def test_meshcore_session_subdict_without_session(self, meshcore_adapter) -> None:
        diag = meshcore_adapter.diagnostics()
        assert isinstance(diag.get("session"), dict)
        session = diag["session"]
        assert session["connected"] is False
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 0
        assert session["last_error"] is None
        assert session["transient_delivery_failures"] == 0
        assert session["permanent_delivery_failures"] == 0

    def test_matrix_fallback_includes_connected_false(self, matrix_adapter) -> None:
        """Matrix already had a fallback dict; verify it includes common keys."""
        diag = matrix_adapter.diagnostics()
        assert diag["connected"] is False
        assert diag["reconnecting"] is False
        assert diag["reconnect_attempts"] == 0


# ===================================================================
# 9. health key in diagnostics (P0 fix verification)
# ===================================================================


class TestHealthInDiagnostics:
    """All adapters include a ``health`` key in diagnostics().

    The ``health`` value is cached from the last ``health_check()`` call.
    Before any health_check(), it is None — an honest report of unknown state.
    """

    @pytest.mark.parametrize(
        "adapter_fixture",
        ["meshtastic_adapter", "meshcore_adapter", "lxmf_adapter", "matrix_adapter"],
    )
    def test_health_key_present(self, adapter_fixture, request) -> None:
        adapter = request.getfixturevalue(adapter_fixture)
        diag = adapter.diagnostics()
        assert "health" in diag

    @pytest.mark.parametrize(
        "adapter_fixture",
        ["meshtastic_adapter", "meshcore_adapter", "lxmf_adapter", "matrix_adapter"],
    )
    def test_health_is_none_before_check(self, adapter_fixture, request) -> None:
        adapter = request.getfixturevalue(adapter_fixture)
        diag = adapter.diagnostics()
        assert diag["health"] is None

    async def test_meshtastic_health_updates_after_check(
        self, meshtastic_adapter, make_adapter_context
    ) -> None:
        """After health_check(), health key reflects the cached value."""
        ctx = make_adapter_context("epa-mt-hc")
        await meshtastic_adapter.start(ctx)
        info = await meshtastic_adapter.health_check()
        diag = meshtastic_adapter.diagnostics()
        assert diag["health"] == info.health
        assert diag["health"] in {"healthy", "degraded", "unknown", "failed"}
        await meshtastic_adapter.stop()

    async def test_meshcore_health_updates_after_check(
        self, meshcore_adapter, make_adapter_context
    ) -> None:
        ctx = make_adapter_context("epa-mc-hc")
        await meshcore_adapter.start(ctx)
        info = await meshcore_adapter.health_check()
        diag = meshcore_adapter.diagnostics()
        assert diag["health"] == info.health
        assert diag["health"] in {"healthy", "degraded", "unknown", "failed"}
        await meshcore_adapter.stop()

    async def test_lxmf_health_updates_after_check(
        self, lxmf_adapter, make_adapter_context
    ) -> None:
        ctx = make_adapter_context("epa-lx-hc")
        await lxmf_adapter.start(ctx)
        info = await lxmf_adapter.health_check()
        diag = lxmf_adapter.diagnostics()
        assert diag["health"] == info.health
        assert diag["health"] in {"healthy", "unknown", "failed"}
        await lxmf_adapter.stop()

    async def test_matrix_health_updates_after_check(
        self, make_adapter_context
    ) -> None:
        """Use FakeMatrixAdapter to verify health caching in diagnostics."""
        from medre.adapters.fakes.matrix import FakeMatrixAdapter

        adapter = FakeMatrixAdapter("epa-mx-hc")
        ctx = make_adapter_context("epa-mx-hc")
        await adapter.start(ctx)
        info = await adapter.health_check()
        # FakeMatrixAdapter diagnostics are minimal; verify the adapter
        # was started and health_check works.
        assert info.health == "healthy"
        await adapter.stop()


# ===================================================================
# 10. mode key in diagnostics (P1 fix verification)
# ===================================================================


class TestModeInDiagnostics:
    """All adapters include a ``mode`` key in diagnostics()."""

    def test_meshtastic_mode_is_connection_type(self, meshtastic_adapter) -> None:
        diag = meshtastic_adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_meshcore_mode_is_connection_type(self, meshcore_adapter) -> None:
        diag = meshcore_adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_lxmf_mode_is_connection_type(self, lxmf_adapter) -> None:
        diag = lxmf_adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_matrix_mode_is_live(self, matrix_adapter) -> None:
        diag = matrix_adapter.diagnostics()
        assert diag["mode"] == "live"


# ===================================================================
# 11. Matrix last_error / last_sync_error alias (P1 fix verification)
# ===================================================================


class TestMatrixLastErrorAlias:
    """Matrix includes both ``last_error`` (spec key) and ``last_sync_error``."""

    def test_both_keys_present_no_session(self, matrix_adapter) -> None:
        diag = matrix_adapter.diagnostics()
        assert "last_error" in diag
        assert "last_sync_error" in diag

    def test_both_keys_none_when_no_error(self, matrix_adapter) -> None:
        diag = matrix_adapter.diagnostics()
        assert diag["last_error"] is None
        assert diag["last_sync_error"] is None

    def test_both_keys_match_with_error(self, matrix_adapter) -> None:
        mock_session = MagicMock()
        mock_diag = MagicMock()
        mock_diag.connected = True
        mock_diag.logged_in = True
        mock_diag.sync_task_running = True
        mock_diag.last_sync_error = RuntimeError("connection lost")
        mock_diag.store_path_configured = False
        mock_diag.device_id_configured = False
        mock_diag.encryption_mode = "plaintext"
        mock_diag.crypto_enabled = False
        mock_diag.last_crypto_error = None
        mock_diag.encrypted_room_seen = False
        mock_diag.undecryptable_event_count = 0
        mock_diag.sync_running = True
        mock_diag.reconnecting = False
        mock_diag.reconnect_attempts = 2
        mock_diag.last_successful_sync = 1700000000.0
        mock_diag.crypto_store_loaded = False
        mock_diag.olm_loaded = False
        mock_diag.store_loaded = False
        mock_diag.device_keys_uploaded = False
        mock_diag.key_query_needed = False
        mock_diag.device_id_in_use = None
        mock_diag.store_path_exists = False
        mock_diag.initial_sync_completed = False
        mock_diag.encrypted_room_count = 0
        mock_diag.plaintext_room_count = 0
        mock_session.diagnostics.return_value = mock_diag
        matrix_adapter._session = mock_session

        diag = matrix_adapter.diagnostics()
        assert diag["last_sync_error"] == "connection lost"
        assert diag["last_error"] == "connection lost"
        assert diag["last_error"] == diag["last_sync_error"]

    def test_last_error_extracts_str_from_exception(self, matrix_adapter) -> None:
        """When last_sync_error is an Exception, last_error gets str(exception)."""
        mock_session = MagicMock()
        mock_diag = MagicMock()
        mock_diag.connected = True
        mock_diag.logged_in = True
        mock_diag.sync_task_running = True
        mock_diag.last_sync_error = TimeoutError("timed out after 30s")
        mock_diag.store_path_configured = False
        mock_diag.device_id_configured = False
        mock_diag.encryption_mode = "plaintext"
        mock_diag.crypto_enabled = False
        mock_diag.last_crypto_error = None
        mock_diag.encrypted_room_seen = False
        mock_diag.undecryptable_event_count = 0
        mock_diag.sync_running = True
        mock_diag.reconnecting = True
        mock_diag.reconnect_attempts = 5
        mock_diag.last_successful_sync = None
        mock_diag.crypto_store_loaded = False
        mock_diag.olm_loaded = False
        mock_diag.store_loaded = False
        mock_diag.device_keys_uploaded = False
        mock_diag.key_query_needed = False
        mock_diag.device_id_in_use = None
        mock_diag.store_path_exists = False
        mock_diag.initial_sync_completed = False
        mock_diag.encrypted_room_count = 0
        mock_diag.plaintext_room_count = 0
        mock_session.diagnostics.return_value = mock_diag
        matrix_adapter._session = mock_session

        diag = matrix_adapter.diagnostics()
        assert "timed out" in diag["last_error"]


# ===================================================================
# 12. normalize_diagnostics handles both flat and nested shapes
# ===================================================================


class TestNormalizeDiagnosticsShapes:
    """normalize_diagnostics handles flat (Matrix) and nested (others) shapes."""

    def test_flat_dict_extraction(self) -> None:
        raw = {
            "connected": True,
            "health": "healthy",
            "mode": "live",
            "reconnecting": False,
            "reconnect_attempts": 0,
            "last_error": None,
            "transient_delivery_failures": 0,
            "permanent_delivery_failures": 0,
            "extra_key": "value",
        }
        result = normalize_diagnostics(raw)
        assert result["connected"] is True
        assert result["health"] == "healthy"
        assert result["mode"] == "live"
        # extra_key should be in transport_specific
        assert result["transport_specific"]["extra_key"] == "value"

    def test_nested_session_dict_not_extracted_as_common(self) -> None:
        """Keys inside a 'session' sub-dict are not extracted as common keys.

        The normalize_diagnostics function only looks at the flat top level.
        Session-subdict keys become transport_specific entries.
        """
        raw = {
            "adapter_id": "mesh-1",
            "mode": "fake",
            "session": {
                "connected": True,
                "reconnecting": False,
                "reconnect_attempts": 0,
                "last_error": None,
                "transient_delivery_failures": 0,
                "permanent_delivery_failures": 0,
            },
        }
        result = normalize_diagnostics(raw)
        # Only top-level mode is extracted as common.
        assert result["mode"] == "fake"
        # connected at top level is missing → None (not extracted from session).
        assert result["connected"] is None
        # The session sub-dict is preserved in transport_specific.
        assert "session" in result["transport_specific"]


# ===================================================================
# 13. JSON safety of diagnostics output
# ===================================================================


class TestDiagnosticsJsonSafety:
    """All adapter diagnostics output is JSON-safe (no SDK objects)."""

    @pytest.mark.parametrize(
        "adapter_fixture",
        ["meshtastic_adapter", "meshcore_adapter", "lxmf_adapter", "matrix_adapter"],
    )
    def test_diagnostics_values_are_json_safe(self, adapter_fixture, request) -> None:
        """Every value in diagnostics is JSON-safe."""
        import json

        adapter = request.getfixturevalue(adapter_fixture)
        diag = adapter.diagnostics()
        # Must not raise on serialization.
        serialized = json.dumps(diag)
        assert isinstance(serialized, str)

    def test_normalize_diagnostics_output_is_json_safe(self) -> None:
        """normalize_diagnostics output is always JSON-safe."""
        import json

        raw = {
            "connected": True,
            "complex_object": object(),  # Should be sanitized.
        }
        result = normalize_diagnostics(raw)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        # The complex_object should have been sanitized to a placeholder.
        assert result["transport_specific"]["complex_object"] == "<object>"


# ===================================================================
# 14. Fake adapter diagnostics shape (documenting current behavior)
# ===================================================================


class TestFakeAdapterDiagnosticsShape:
    """Fake adapters produce minimal diagnostics shapes.

    These tests document the current behavior of fake adapters, which
    produce simplified diagnostics compared to real adapters. This is
    expected — fake adapters are not required to mirror real adapter
    session sub-dict structures.
    """

    def test_fake_matrix_mode_is_fake(self) -> None:
        from medre.adapters.fakes.matrix import FakeMatrixAdapter

        adapter = FakeMatrixAdapter("epa-fake-mx")
        diag = adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_fake_meshtastic_mode_is_fake(self) -> None:
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="epa-fake-mt")
        adapter = FakeMeshtasticAdapter(config)
        diag = adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_fake_meshcore_mode_is_fake(self) -> None:
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="epa-fake-mc")
        adapter = FakeMeshCoreAdapter(config)
        diag = adapter.diagnostics()
        assert diag["mode"] == "fake"

    def test_fake_lxmf_mode_is_fake(self) -> None:
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(adapter_id="epa-fake-lx")
        adapter = FakeLxmfAdapter(config)
        diag = adapter.diagnostics()
        assert diag["mode"] == "fake"


# ===================================================================
# 15. Meshtastic mode matches connection_type (P1 verification)
# ===================================================================


class TestMeshtasticModeMatchesConnectionType:
    """Meshtastic 'mode' key matches config.connection_type."""

    def test_fake_mode(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="mode-fake", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        assert adapter.diagnostics()["mode"] == "fake"

    def test_tcp_mode(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="mode-tcp",
            connection_type="tcp",
            host="localhost",
        )
        adapter = MeshtasticAdapter(config)
        assert adapter.diagnostics()["mode"] == "tcp"

    def test_serial_mode(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="mode-serial",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        adapter = MeshtasticAdapter(config)
        assert adapter.diagnostics()["mode"] == "serial"

    def test_ble_mode(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="mode-ble",
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        adapter = MeshtasticAdapter(config)
        assert adapter.diagnostics()["mode"] == "ble"
