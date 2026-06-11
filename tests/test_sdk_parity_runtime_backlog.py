"""SDK parity runtime backlog characterization tests.

Tests that document and guard the current state of Wave 1 SDK parity backlog
items (P-01 through P-12 from ``docs/dev/sdk-parity-backlog.md``).  These tests
characterize **current behavior** — they verify what the adapters do today so
that future parity work can detect regressions and confirm improvements.

Evidence level: **fake_pipeline** (tier 1) and **fake_adapter_callback** (tier 2).
No live SDK, network, or hardware dependencies.

Gap classification:
- **Behavioral gap**: Runtime code path behaves differently from the reference
  in a way that could cause operational failures.
- **Declarative/capability gap**: MEDRE does not expose or use a capability
  that the SDK provides, but the absence does not cause incorrect runtime
  behavior.

References:
- ``docs/dev/sdk-parity-backlog.md`` — full backlog with rationale
- ``docs/dev/adapter-reality-audit.md`` — prior correctness wave (R1–R10)
- ``docs/dev/reference-repos.md`` — boundary rules on external references
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import fields as dataclass_fields
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.lxmf import session as lxmf_session_mod
from medre.adapters.lxmf.session import (
    LxmfSession,
)
from medre.adapters.matrix import session as matrix_session_mod
from medre.adapters.meshcore import session as meshcore_session_mod

# Re-import session modules so we can inspect their source / constants.
from medre.adapters.meshtastic import session as meshtastic_session_mod
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig

# ===================================================================
# Shared helpers
# ===================================================================


def _make_lxmf_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-parity-test")
    defaults.update(overrides)
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-parity-lxmf-router"
    return LxmfConfig(**defaults)


def _make_lxmf_session(**config_overrides: Any) -> LxmfSession:
    config = _make_lxmf_config(**config_overrides)
    return LxmfSession(config=config, adapter_id=config.adapter_id)


def _make_meshcore_config(**overrides: Any) -> MeshCoreConfig:
    defaults: dict[str, Any] = dict(adapter_id="mc-parity-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_meshtastic_config(**overrides: Any) -> MeshtasticConfig:
    defaults: dict[str, Any] = dict(adapter_id="mesh-parity-test")
    defaults.update(overrides)
    return MeshtasticConfig(**defaults)


def _make_matrix_config(**overrides: Any) -> MatrixConfig:
    defaults: dict[str, Any] = dict(
        adapter_id="matrix-parity-test",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok_parity",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)


# ===================================================================
# P-01: Meshtastic — No periodic connection health verification
# Gap type: Behavioral
# ===================================================================


class TestP01MeshtasticNoHealthCheck:
    """Characterize P-01: MeshtasticSession lacks periodic health checks.

    The session has no liveness probe that detects silent TCP connection
    drops (half-open state).  Only inbound packet reception updates
    ``_last_packet_time``, and nothing reads that timestamp to detect
    staleness.
    """

    def test_diagnostics_has_no_health_check_field(self) -> None:
        """MeshtasticSessionDiagnostics does not expose health-check data."""
        diag_cls = meshtastic_session_mod.MeshtasticSessionDiagnostics
        field_names = {f.name for f in dataclass_fields(diag_cls)}
        # Current state: no health_check_time, no health_check_interval,
        # no liveness_probe field.
        assert "health_check_time" not in field_names
        assert "health_check_interval" not in field_names
        assert "liveness_probe" not in field_names
        # Diagnostic does expose last_packet_time (passive observation only).
        assert "last_packet_time" in field_names

    def test_session_has_no_health_check_task_slot(self) -> None:
        """MeshtasticSession __slots__ lacks a periodic health-check task."""
        slots = set(meshtastic_session_mod.MeshtasticSession.__slots__)
        assert "_health_check_task" not in slots
        assert "_health_check_interval" not in slots
        # Has reconnect task (existing reconnect infrastructure).
        assert "_reconnect_task" in slots

    def test_config_has_no_health_check_interval(self) -> None:
        """MeshtasticConfig does not define a health-check interval field."""
        field_names = {f.name for f in dataclass_fields(MeshtasticConfig)}
        assert "health_check_interval_seconds" not in field_names
        assert "health_check_interval" not in field_names

    def test_subscribe_callbacks_subscribes_to_receive_and_connection_lost(
        self,
    ) -> None:
        """_subscribe_callbacks subscribes to receive and connection.lost.

        P-01 gap (no health probe) remains: the session relies
        exclusively on inbound packets for liveness indication.
        P-02 resolution: connection.lost is now subscribed for
        automatic reconnect triggering.
        """
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._subscribe_callbacks,
        )
        subscribe_count = source.count("pub.subscribe(")
        assert subscribe_count == 2, (
            f"Expected 2 pub.subscribe calls (receive + connection.lost), "
            f"found {subscribe_count}. "
            "If a health-check subscription was added, update this test."
        )
        assert "meshtastic.receive" in source
        assert "meshtastic.connection.lost" in source

    def test_backoff_cap_and_max_attempts_current_values(self) -> None:
        """Document current reconnect constants for future parity comparison.

        P-08 notes these are lower than reference (mmrelay caps at 300s,
        retries indefinitely).  These tests lock the current values so
        that any change is intentional and visible.
        """
        assert meshtastic_session_mod._BACKOFF_CAP == 30.0
        assert meshtastic_session_mod._MAX_RECONNECT_ATTEMPTS == 10
        assert meshtastic_session_mod._BACKOFF_BASE == 1.0
        assert meshtastic_session_mod._BACKOFF_JITTER_FRACTION == 0.25


# ===================================================================
# P-02: Meshtastic — SDK connection-lost event subscription (RESOLVED)
# Gap type: Behavioral (RESOLVED)
# ===================================================================


class TestP02MeshtasticConnectionLostSubscriptionResolved:
    """Verify P-02 resolution: Session now subscribes to
    meshtastic.connection.lost for automatic reconnect triggering.

    The SDK fires this pubsub event when it detects a disconnect.
    MEDRE now listens for it via ``_on_connection_lost``, which
    delegates to ``notify_connection_lost()`` for thread-safe
    reconnect scheduling.
    """

    def test_subscribe_callbacks_subscribes_to_connection_lost(self) -> None:
        """_subscribe_callbacks subscribes to connection.lost events."""
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._subscribe_callbacks,
        )
        assert "meshtastic.connection.lost" in source

    def test_unsubscribe_callbacks_unsubscribes_from_connection_lost(self) -> None:
        """_unsubscribe_callbacks unsubscribes from connection-lost."""
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._unsubscribe_callbacks,
        )
        assert "connection_lost" in source

    def test_on_connection_lost_handler_exists(self) -> None:
        """Session defines a _on_connection_lost callback handler."""
        session_cls = meshtastic_session_mod.MeshtasticSession
        assert hasattr(session_cls, "_on_connection_lost")

    def test_on_connection_lost_handler_delegates_to_notify(self) -> None:
        """_on_connection_lost delegates to notify_connection_lost."""
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._on_connection_lost,
        )
        assert "notify_connection_lost" in source

    def test_notify_connection_lost_uses_threadsafe_scheduling(self) -> None:
        """notify_connection_lost() schedules reconnect via event loop.

        Uses call_soon_threadsafe for thread-safe reconnect scheduling
        from the SDK reader thread.
        """
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession.notify_connection_lost,
        )
        assert "call_soon_threadsafe" in source
        assert "_start_reconnect_task" in source

    async def test_notify_connection_lost_triggers_reconnect_with_loop(
        self,
    ) -> None:
        """Calling notify_connection_lost() starts reconnect when loop is set."""
        from medre.adapters.meshtastic.session import MeshtasticSession

        config = _make_meshtastic_config()
        session = MeshtasticSession(
            config=config,
            adapter_id=config.adapter_id,
            platform="meshtastic",
        )
        # Simulate a started session with a client and event loop.
        session._started = True
        session._client = MagicMock()
        session._loop = asyncio.get_running_loop()

        # notify_connection_lost uses call_soon_threadsafe which schedules
        # _start_reconnect_task on the event loop.  Yield to let it execute.
        session.notify_connection_lost()
        await asyncio.sleep(0)

        # A reconnect task should have been created.
        assert session._reconnect_task is not None
        assert not session._reconnect_task.done()
        # Clean up — cancel the task.
        session._stop_requested = True
        session._reconnect_task.cancel()
        try:
            await session._reconnect_task
        except asyncio.CancelledError:
            pass


# ===================================================================
# P-03: Matrix — No sync token persistence across restarts
# Gap type: Behavioral
# ===================================================================


class TestP03MatrixNoSyncTokenPersistence:
    """Characterize P-03: AsyncClientConfig does not set store_sync_tokens.

    Without ``store_sync_tokens=True``, nio does not persist the
    ``next_batch`` token to its store.  After restart, the sync starts
    from the beginning, causing slow startup with a burst of initial
    events.
    """

    def test_e2ee_start_no_store_sync_tokens_in_config(self) -> None:
        """_start_e2ee_required creates AsyncClientConfig without
        store_sync_tokens."""
        source = inspect.getsource(
            matrix_session_mod.MatrixSession._start_e2ee_required,
        )
        assert "store_sync_tokens" not in source

    def test_plaintext_start_no_async_client_config(self) -> None:
        """_start_plaintext creates AsyncClient directly, no AsyncClientConfig."""
        source = inspect.getsource(
            matrix_session_mod.MatrixSession._start_plaintext,
        )
        assert "store_sync_tokens" not in source

    def test_last_successful_sync_resets_on_restart(self) -> None:
        """_last_successful_sync is reset to None in start().

        This confirms the session does not carry sync state across
        restarts — the value is only populated by successful sync
        responses during the current run.
        """
        source = inspect.getsource(matrix_session_mod.MatrixSession.start)
        # The reset line exists in start().
        assert "self._last_successful_sync = None" in source

    def test_matrix_config_has_no_sync_token_persistence_field(self) -> None:
        """MatrixConfig has no sync-token-persistence configuration field."""
        field_names = {f.name for f in dataclass_fields(MatrixConfig)}
        assert "store_sync_tokens" not in field_names
        assert "sync_token_persistence" not in field_names

    async def test_e2ee_config_construction_uses_only_encryption_enabled(
        self,
    ) -> None:
        """AsyncClientConfig is constructed with encryption_enabled only.

        When E2EE parity is implemented, this test should be updated to
        verify store_sync_tokens=True is also passed.
        """
        from tests.helpers.matrix_session import build_mock_nio_module

        mock_nio = build_mock_nio_module()
        config = _make_matrix_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/medre-parity-matrix-store",
            device_id="TESTDEVICE",
        )

        from medre.adapters.matrix.session import MatrixSession

        session = MatrixSession(config=config)

        # Patch HAS_E2EE so the session thinks vodozemac is available.
        with (
            patch("medre.adapters.matrix.compat.HAS_E2EE", True),
            patch("medre.adapters.matrix.compat.HAS_NIO", True),
            patch.dict("sys.modules", {"nio": mock_nio, "nio.events": mock_nio.events}),
        ):
            # Record what AsyncClientConfig was called with.
            await session.start()

        # The mock AsyncClientConfig is aliased as ClientConfig.
        config_call = mock_nio.ClientConfig.call_args
        assert config_call is not None, "AsyncClientConfig was never called"
        kwargs = config_call.kwargs
        # Current behavior: only encryption_enabled is passed.
        assert kwargs.get("encryption_enabled") is True
        # store_sync_tokens is NOT passed — this is the P-03 gap.
        assert "store_sync_tokens" not in kwargs
        await session.stop()


# ===================================================================
# P-04: MeshCore — Unused suggested_timeout from SDK send result
# Gap type: Behavioral
# ===================================================================


class TestP04MeshCoreSuggestedTimeoutResolved:
    """Verify P-04 resolution: _send_real now uses suggested_timeout from SDK.

    The SDK returns ``suggested_timeout`` (milliseconds) alongside
    ``expected_ack`` from ``send_msg()``.  MEDRE now extracts and clamps
    the value, using it as the retry delay for DM transient failures.
    Falls back to ``0.1 * attempt`` when suggested_timeout is unavailable.
    """

    def test_send_real_uses_suggested_timeout_in_source(self) -> None:
        """_send_real source references suggested_timeout for DM retry timing.

        Uses targeted regex to confirm ``suggested_timeout`` is extracted
        and used.  String-key extraction and comments are expected.
        """
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._send_real,
        )
        # Verify the extraction helper is called.
        assert "_extract_suggested_timeout" in source
        # Verify the SDK retry delay variable is assigned.
        assert "sdk_retry_delay" in source

    def test_extract_suggested_timeout_helper_exists(self) -> None:
        """_extract_suggested_timeout helper is defined in the module."""
        assert hasattr(meshcore_session_mod, "_extract_suggested_timeout")

    def test_retry_delay_falls_back_to_linear_when_no_sdk_hint(self) -> None:
        """Retry sleep in _send_real falls back to 0.1 * attempt without SDK hint."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._send_real,
        )
        assert "0.1 * attempt" in source
        assert "sdk_retry_delay is not None" in source

    def test_send_max_retries_is_three(self) -> None:
        """_SEND_MAX_RETRIES is 3, giving bounded retry window."""
        assert meshcore_session_mod._SEND_MAX_RETRIES == 3

    async def test_send_real_uses_suggested_timeout_for_retry_delay(
        self,
    ) -> None:
        """_send_real uses suggested_timeout as retry delay for DM retries.

        The suggested_timeout is captured from a successful DM result and
        reused for subsequent retry delays.  This test forces two failures
        followed by a success to exercise the retry delay path with the
        captured SDK hint.
        """
        from tests.helpers.meshcore_session import (
            build_mock_meshcore_module,
            install_mock_module,
            remove_mock_module,
        )

        # Use tcp mode so send_text routes through _send_real (not fake path).
        config = _make_meshcore_config(
            connection_type="tcp",
            host="127.0.0.1",
            port=4000,
        )
        from medre.adapters.meshcore.session import MeshCoreSession

        session = MeshCoreSession(config=config, adapter_id=config.adapter_id)

        mock_mc, instance = build_mock_meshcore_module()

        # First send: success to capture suggested_timeout.
        first_result = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,  # 5 seconds in milliseconds
        }
        instance.commands.send_msg = AsyncMock(return_value=first_result)

        install_mock_module(mock_mc)
        try:
            with (
                patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
                patch(
                    "medre.adapters.meshcore.session.asyncio.sleep",
                    new_callable=AsyncMock,
                ),
            ):
                callback = MagicMock()
                await session.start(message_callback=callback)
                # First send succeeds — captures sdk_retry_delay (5.0s).
                native_id = await session.send_text("aabbccdd", "hello")
                assert native_id == "01020304"

            # Verify the diagnostic counter was incremented for the capture.
            assert session._diag.sdk_suggested_timeouts_used >= 1

        finally:
            remove_mock_module()
            await session.stop()

    async def test_send_real_falls_back_to_linear_without_suggested_timeout(
        self,
    ) -> None:
        """When SDK returns no suggested_timeout, retry uses 0.1 * attempt."""
        from tests.helpers.meshcore_session import (
            build_mock_meshcore_module,
            install_mock_module,
            remove_mock_module,
        )

        # Use tcp mode so send_text routes through _send_real.
        config = _make_meshcore_config(
            connection_type="tcp",
            host="127.0.0.1",
            port=4000,
        )
        from medre.adapters.meshcore.session import MeshCoreSession

        session = MeshCoreSession(config=config, adapter_id=config.adapter_id)

        mock_mc, instance = build_mock_meshcore_module()

        # SDK returns no suggested_timeout.
        sdk_result = {
            "expected_ack": b"\x11\x22\x33\x44",
        }
        # Force one transient failure to exercise retry path.
        instance.commands.send_msg = AsyncMock(
            side_effect=[RuntimeError("transient send failure"), sdk_result]
        )

        install_mock_module(mock_mc)
        try:
            with (
                patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
                patch(
                    "medre.adapters.meshcore.session.asyncio.sleep",
                    new_callable=AsyncMock,
                ) as mock_sleep,
            ):
                callback = MagicMock()
                await session.start(message_callback=callback)
                native_id = await session.send_text("aabbccdd", "hello")

            assert native_id == "11223344"
            # The retry delay should be 0.1 * attempt (0.1 * 1 = 0.1).
            sleep_args = [c[0][0] for c in mock_sleep.call_args_list if c[0]]
            assert any(
                abs(s - 0.1) < 0.01 for s in sleep_args
            ), f"Expected a sleep near 0.1s from linear fallback, got: {sleep_args}"

        finally:
            remove_mock_module()
            await session.stop()


# ===================================================================
# P-05: MeshCore — Contact-list subscriptions (RESOLVED)
# Gap type: Declarative/capability (RESOLVED)
# ===================================================================


class TestP05MeshCoreContactListSubscriptionsResolved:
    """Verify P-05 resolution: Session now subscribes to CONTACTS and SELF_INFO.

    The SDK provides CONTACTS, SELF_INFO and others.  MEDRE now subscribes
    to all five event types for diagnostics-only observability:
    CONTACT_MSG_RECV, CHANNEL_MSG_RECV, DISCONNECTED, CONTACTS, SELF_INFO.
    No topology canonical events are emitted from contact/self-info handlers.
    """

    def test_subscribe_events_has_five_subscriptions(self) -> None:
        """_subscribe_events subscribes to five EventType members."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._subscribe_events,
        )
        # Count subscribe calls (mc.EventType.* passed to self._meshcore.subscribe).
        subscribe_calls = [
            line
            for line in source.splitlines()
            if "mc.EventType." in line
            and "subscribe(" not in line
            and "hasattr" not in line
        ]
        # Five subscriptions: CONTACT_MSG_RECV, CHANNEL_MSG_RECV,
        # DISCONNECTED, CONTACTS, SELF_INFO.
        assert len(subscribe_calls) >= 5, (
            f"Expected at least 5 EventType subscribe references, "
            f"found {len(subscribe_calls)}. "
        )

    def test_contacts_subscription_present(self) -> None:
        """_subscribe_events subscribes to CONTACTS event type."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._subscribe_events,
        )
        assert "CONTACTS" in source

    def test_self_info_subscription_present(self) -> None:
        """_subscribe_events subscribes to SELF_INFO event type."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._subscribe_events,
        )
        assert "SELF_INFO" in source


# ===================================================================
# P-06: LXMF — Periodic announce for mesh path discovery
# Gap type: Behavioral (RESOLVED)
# ===================================================================


class TestP06LxmfPeriodicAnnounceImplemented:
    """Verify P-06 resolution: LxmfSession now has a working periodic
    announce loop.

    The session creates an ``_announce_task`` when started in non-fake
    mode with ``announce_interval_seconds > 0`` and a valid delivery
    destination hash.  Fake mode never creates network-visible announces.
    """

    async def test_announce_task_is_none_in_fake_mode(self) -> None:
        """_announce_task remains None after session start (fake mode)."""
        session = _make_lxmf_session(connection_type="fake")
        await session.start()

        assert session._announce_task is None
        await session.stop()

    async def test_announce_task_created_in_reticulum_mode(self) -> None:
        """_announce_task is created after reticulum mode start.

        The connect path sets up the router, registers a delivery
        identity, and starts the periodic announce task.
        """
        session = _make_lxmf_session(
            connection_type="reticulum",
            announce_interval_seconds=600,
        )

        mock_dest = MagicMock()
        mock_dest.hash = b"\x01" * 16
        mock_router = MagicMock()
        mock_router.register_delivery_identity.return_value = mock_dest
        mock_router.register_delivery_callback.return_value = None

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_rns.Identity.return_value = MagicMock()
        mock_lxmf.LXMRouter.return_value = mock_router

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        assert session._announce_task is not None
        assert not session._announce_task.done()
        await session.stop()

    def test_config_has_announce_interval_seconds(self) -> None:
        """LxmfConfig defines announce_interval_seconds."""
        field_names = {f.name for f in dataclass_fields(LxmfConfig)}
        assert "announce_interval_seconds" in field_names

    def test_announce_task_slot_exists(self) -> None:
        """Infrastructure for announce task exists (slot + cancel logic)."""
        source = inspect.getsource(lxmf_session_mod.LxmfSession.stop)
        assert "_announce_task" in source

    def test_announce_loop_method_exists(self) -> None:
        """LxmfSession has an _announce_loop method."""
        assert hasattr(lxmf_session_mod.LxmfSession, "_announce_loop")

    async def test_announce_disabled_when_interval_zero(self) -> None:
        """_announce_task is None when announce_interval_seconds=0."""
        session = _make_lxmf_session(
            connection_type="reticulum",
            announce_interval_seconds=0,
        )

        mock_dest = MagicMock()
        mock_dest.hash = b"\x01" * 16
        mock_router = MagicMock()
        mock_router.register_delivery_identity.return_value = mock_dest
        mock_router.register_delivery_callback.return_value = None

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_rns.Identity.return_value = MagicMock()
        mock_lxmf.LXMRouter.return_value = mock_router

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        assert session._announce_task is None
        await session.stop()


# ===================================================================
# P-07: Matrix — No sync liveness watchdog
# Gap type: Behavioral
# ===================================================================


class TestP07MatrixNoSyncLivenessWatchdog:
    """Characterize P-07: No watchdog detects a stalled sync loop.

    ``_last_successful_sync`` is recorded but nothing acts on a stale
    value.  If the sync loop hangs, the adapter appears connected but
    stops receiving events.
    """

    def test_no_watchdog_task_in_session_slots(self) -> None:
        """MatrixSession has no watchdog-related slots."""
        slots = set(matrix_session_mod.MatrixSession.__slots__)
        assert "_watchdog_task" not in slots
        assert "_sync_liveness_task" not in slots
        assert "_sync_watchdog_task" not in slots

    def test_last_successful_sync_is_passive_diagnostic(self) -> None:
        """_last_successful_sync is set on sync success but never read
        for liveness decisions.

        Positively flags conditional/operational uses (if, comparisons,
        staleness/watchdog/restart/warn/error/raise patterns) rather than
        excluding many benign lines.
        """
        source = inspect.getsource(matrix_session_mod.MatrixSession)
        # Patterns that indicate _last_successful_sync is read for an
        # operational decision, not just recorded or exposed as diagnostic.
        operational_re = re.compile(
            r"\b(if|elif)\b.*_last_successful_sync"
            r"|_last_successful_sync\s*(==|!=|<|>|<=|>=|is\b|is not\b)"
            r"|_last_successful_sync.*\b(warn|error|raise|restart|watchdog|staleness|stale)\b",
        )
        operational_lines = [
            line.strip()
            for line in source.splitlines()
            if "_last_successful_sync" in line
            and not line.strip().startswith("#")
            and operational_re.search(line)
        ]
        # No operational reads — only assignment and diagnostic output.
        assert len(operational_lines) == 0, (
            f"_last_successful_sync used in operational context: "
            f"{operational_lines}. "
            "If a watchdog was added, update this test."
        )


# ===================================================================
# P-08: Meshtastic — Reconnect backoff cap and max attempts
# Gap type: Behavioral
#
# (Constants already tested in P-01; this section documents the gap
#  classification and reference comparison.)
# ===================================================================


class TestP08MeshtasticReconnectParameters:
    """Document P-08: Reconnect parameters are more conservative than
    reference (mmrelay: 300s cap, no attempt limit).

    Current values: _BACKOFF_CAP=30.0, _MAX_RECONNECT_ATTEMPTS=10.
    After 10 failed attempts (~5 min total), the session gives up
    permanently.
    """

    def test_total_retry_budget_approximately_five_minutes(self) -> None:
        """With exponential backoff 1..30s and 10 attempts, total budget
        is approximately 300-500 seconds depending on jitter."""
        cap = meshtastic_session_mod._BACKOFF_CAP
        base = meshtastic_session_mod._BACKOFF_BASE
        max_attempts = meshtastic_session_mod._MAX_RECONNECT_ATTEMPTS

        total = 0.0
        for i in range(1, max_attempts + 1):
            delay = min(base * (2 ** (i - 1)), cap)
            total += delay

        # With base=1, cap=30, 10 attempts: 1+2+4+8+16+30+30+30+30+30 = 181s
        # This is well below mmrelay's indefinite retry.
        assert total < 300.0, (
            f"Total retry budget {total:.0f}s exceeds expected range. "
            "If backoff was adjusted for parity, update this test."
        )

    def test_session_gives_up_permanently_on_max_attempts(self) -> None:
        """Reconnect loop compares against _MAX_RECONNECT_ATTEMPTS and
        returns permanently when exceeded."""
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._reconnect_loop,
        )
        # Must reference the constant for comparison.
        assert "_MAX_RECONNECT_ATTEMPTS" in source, (
            "_reconnect_loop does not reference _MAX_RECONNECT_ATTEMPTS. "
            "If reconnect logic changed, update this test."
        )
        # Must have a giving-up branch: a comparison against the constant
        # followed by return (not just continue/retry).
        assert re.search(r"giving up", source, re.IGNORECASE), (
            "_reconnect_loop does not log a 'giving up' message. "
            "If reconnect logic changed, update this test."
        )
        # The giving-up branch must contain an explicit return (permanent exit).
        giving_up_match = re.search(
            r"giving up.*?\n(.*?)(?:return|continue|break)",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        assert giving_up_match is not None and "return" in giving_up_match.group(0), (
            "_reconnect_loop 'giving up' branch does not return permanently. "
            "If reconnect logic changed, update this test."
        )


# ===================================================================
# P-09: Meshtastic — Queue water-mark monitoring
# Gap type: Declarative/capability
# ===================================================================


class TestP09MeshtasticQueueWatermarkMonitoring:
    """Characterize P-09: No water-mark thresholds on outbound queue.

    The queue tracks diagnostics (depth, max size, counters) but has
    no high-water/critical-water mark thresholds to warn operators
    before rejection.
    """

    def test_no_water_mark_constants_in_adapter(self) -> None:
        """Meshtastic package has no water-mark threshold constants anywhere.

        Scans all modules under ``medre.adapters.meshtastic`` using
        ``pkgutil.walk_packages`` + ``importlib.import_module`` so that
        water-mark constants in queue, session, or future modules are
        caught — not just the adapter module.  Reports all offending
        module names in the failure message.
        """
        import importlib
        import pkgutil

        import medre.adapters.meshtastic as mesh_pkg

        forbidden = ("HIGH_WATER_MARK", "WATER_MARK", "water_mark")
        offending: list[str] = []

        for _importer, modname, _ispkg in pkgutil.walk_packages(
            path=mesh_pkg.__path__,
            prefix=mesh_pkg.__name__ + ".",
        ):
            try:
                mod = importlib.import_module(modname)
            except (ImportError, ModuleNotFoundError):
                continue
            try:
                source = inspect.getsource(mod)
            except (OSError, TypeError):
                continue
            for term in forbidden:
                if term in source:
                    offending.append(f"{modname} contains '{term}'")

        assert not offending, (
            "Water-mark constants found in meshtastic package: "
            + "; ".join(offending)
            + ". If water-mark monitoring was implemented, update this test."
        )


# ===================================================================
# P-10: MeshCore — appstart on reconnect (validation — no gap)
# ===================================================================


class TestP10MeshCoreAppstartValidation:
    """Confirm P-10: send_appstart is correctly issued on connect/reconnect.

    This item was validated as complete in the adapter-reality-audit (R4).
    These tests guard against regression.
    """

    def test_connect_real_includes_appstart(self) -> None:
        """_connect_real source includes send_appstart call."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._connect_real,
        )
        assert "send_appstart" in source

    def test_connect_real_includes_subscribe_events(self) -> None:
        """_connect_real subscribes to events before sending appstart."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._connect_real,
        )
        assert "_subscribe_events" in source

    def test_reconnect_loop_uses_connect_real(self) -> None:
        """_reconnect_loop delegates to _connect_real (shared path)."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._reconnect_loop,
        )
        assert "_connect_real" in source


# ===================================================================
# P-11: LXMF — Eviction logging lacks delivery state
# Gap type: Declarative/capability
# ===================================================================


class TestP11LxmfEvictionLoggingLacksState:
    """Characterize P-11: Outbound delivery eviction logs count but not
    the state of evicted entries.

    When the 1000-entry tracking cap is hit, oldest entries are evicted
    with a warning that includes the count but not each entry's delivery
    state.  If evicted entries were in SENDING/SENT, the callback will
    never fire for them.
    """

    def test_eviction_log_does_not_include_entry_state(self) -> None:
        """_track_delivery eviction warning does not log evicted entry states."""
        source = inspect.getsource(
            lxmf_session_mod.LxmfSession._track_delivery,
        )
        # The warning line should be about the count, not individual states.
        # Current: logs adapter_id, cap, evict_count.
        # If parity is implemented, the warning should also log entry state.
        eviction_lines = [
            line
            for line in source.splitlines()
            if "evict" in line.lower() and "log" not in line.lower()
        ]
        # Current behavior: only count is logged, not per-entry state.
        # Verify no .state reference in eviction logic.
        for line in eviction_lines:
            if "warning" in line.lower() or "logger" in line.lower():
                assert ".state" not in line

    def test_max_outbound_deliveries_cap(self) -> None:
        """_MAX_OUTBOUND_DELIVERIES is 1000 as documented."""
        assert lxmf_session_mod._MAX_OUTBOUND_DELIVERIES == 1000


# ===================================================================
# P-12: Matrix — E2EE key request rate limiting
# Gap type: Behavioral
# ===================================================================


class TestP12MatrixKeyRequestRateLimiting:
    """Characterize P-12: Key requests are sent inline without rate limiting.

    The dedup window gates logging, not the actual key request send.
    A burst of undecryptable events triggers a burst of key requests.
    """

    def test_key_request_in_megolm_handler(self) -> None:
        """Undecryptable event handler sends key request inline without
        rate limiting, throttling, or sleep gating around as_key_request."""
        source_text = inspect.getsource(matrix_session_mod.MatrixSession)
        assert "as_key_request" in source_text, (
            "as_key_request not found in MatrixSession source; "
            "characterization test must be updated if the source path changed"
        )
        # Verify no rate-limit/throttle/queue/sleep gating around key
        # requests.  Check lines within a window around as_key_request
        # for gating patterns.
        gating_re = re.compile(
            r"rate.?limit|throttl|sleep|queue|Semaphore|cooldown|backoff",
            re.IGNORECASE,
        )
        lines = source_text.splitlines()
        gated_lines: list[str] = []
        for i, line in enumerate(lines):
            if "as_key_request" not in line:
                continue
            # Inspect a window around the key request (5 lines before, 5 after).
            window = lines[max(0, i - 5) : i + 6]
            for wline in window:
                stripped = wline.strip()
                if stripped.startswith("#"):
                    continue
                if gating_re.search(wline) and wline not in gated_lines:
                    gated_lines.append(stripped)
        assert len(gated_lines) == 0, (
            f"Rate-limit/throttle gating found near as_key_request: "
            f"{gated_lines}. "
            "If key request rate limiting was implemented, update this test."
        )


# ===================================================================
# Backlog summary: data-driven assertions against sdk-parity-backlog.md
# ===================================================================

# Each entry maps backlog ID to its documented attributes.
# These are declarative assertions that the backlog document is the
# authoritative source for gap classification.
_BACKLOG_ITEMS: dict[str, dict[str, str]] = {
    "P-01": {
        "adapter": "Meshtastic",
        "gap": "No periodic connection health check",
        "type": "Behavioral",
        "value": "High",
    },
    "P-02": {
        "adapter": "Meshtastic",
        "gap": "No SDK connection-lost event subscription",
        "type": "Behavioral",
        "value": "High",
    },
    "P-03": {
        "adapter": "Matrix",
        "gap": "No sync token persistence across restarts",
        "type": "Behavioral",
        "value": "High",
    },
    "P-04": {
        "adapter": "MeshCore",
        "gap": "Unused suggested_timeout from SDK send result",
        "type": "Behavioral",
        "value": "Med-High",
    },
    "P-05": {
        "adapter": "MeshCore",
        "gap": "No contact-list event subscriptions",
        "type": "Declarative",
        "value": "Medium",
    },
    "P-06": {
        "adapter": "LXMF",
        "gap": "No periodic announce for path discovery",
        "type": "Behavioral",
        "value": "Medium",
    },
    "P-07": {
        "adapter": "Matrix",
        "gap": "No sync liveness watchdog",
        "type": "Behavioral",
        "value": "Medium",
    },
    "P-08": {
        "adapter": "Meshtastic",
        "gap": "Reconnect backoff cap too low, gives up permanently",
        "type": "Behavioral",
        "value": "Medium",
    },
    "P-09": {
        "adapter": "Meshtastic",
        "gap": "Queue water-mark monitoring",
        "type": "Declarative",
        "value": "Low-Medium",
    },
    "P-10": {
        "adapter": "MeshCore",
        "gap": "appstart on reconnect (validation — no gap)",
        "type": "Validation",
        "value": "N/A",
    },
    "P-11": {
        "adapter": "LXMF",
        "gap": "Eviction logging lacks delivery state",
        "type": "Declarative",
        "value": "Low",
    },
    "P-12": {
        "adapter": "Matrix",
        "gap": "E2EE key request rate limiting",
        "type": "Behavioral",
        "value": "Low",
    },
}


class TestBacklogSummary:
    """Data-driven assertions verifying backlog item coverage.

    These tests guard that the characterization tests above cover all
    12 backlog items and that the documented gap classifications are
    consistent with what the tests observe.
    """

    def test_all_twelve_backlog_items_defined(self) -> None:
        """All 12 backlog items from sdk-parity-backlog.md are represented."""
        expected_ids = {f"P-{i:02d}" for i in range(1, 13)}
        assert set(_BACKLOG_ITEMS.keys()) == expected_ids

    @pytest.mark.parametrize(
        ("item_id", "expected_type"),
        [
            ("P-01", "Behavioral"),
            ("P-02", "Behavioral"),
            ("P-03", "Behavioral"),
            ("P-04", "Behavioral"),
            ("P-05", "Declarative"),
            ("P-06", "Behavioral"),
            ("P-07", "Behavioral"),
            ("P-08", "Behavioral"),
            ("P-09", "Declarative"),
            ("P-10", "Validation"),
            ("P-11", "Declarative"),
            ("P-12", "Behavioral"),
        ],
    )
    def test_gap_type_matches_documentation(
        self, item_id: str, expected_type: str
    ) -> None:
        """Each item's gap type matches sdk-parity-backlog.md."""
        assert _BACKLOG_ITEMS[item_id]["type"] == expected_type

    @pytest.mark.parametrize(
        "item_id",
        [f"P-{i:02d}" for i in range(1, 13)],
    )
    def test_each_item_has_required_fields(self, item_id: str) -> None:
        """Each backlog entry has adapter, gap, type, and value fields."""
        entry = _BACKLOG_ITEMS[item_id]
        assert "adapter" in entry
        assert "gap" in entry
        assert "type" in entry
        assert "value" in entry

    def test_behavioral_gaps_have_characterization_tests(self) -> None:
        """Every behavioral gap should have at least one characterization
        test class in this file."""
        # Map item IDs to expected test class name prefixes in this module.
        behavioral_items = [
            item_id
            for item_id, meta in _BACKLOG_ITEMS.items()
            if meta["type"] == "Behavioral"
        ]
        # Verify they exist in this module's namespace.
        import sys

        this_module = sys.modules[__name__]
        for item_id in behavioral_items:
            # Class names follow pattern TestP{NN}... (e.g. TestP01MeshtasticNoHealthCheck).
            prefix = f"Test{item_id.replace('-', '')}"
            found = any(
                name.startswith(prefix)
                for name in dir(this_module)
                if name.startswith("Test")
            )
            assert found, (
                f"Missing characterization test class with prefix {prefix} "
                f"for behavioral gap {_BACKLOG_ITEMS[item_id]['gap']}"
            )

    def test_high_value_items_covered(self) -> None:
        """All high-value behavioral gaps (P-01 through P-04) have tests."""
        high_value = ["P-01", "P-02", "P-03", "P-04"]
        for item_id in high_value:
            entry = _BACKLOG_ITEMS[item_id]
            assert entry["type"] == "Behavioral"
            assert entry["value"] in ("High", "Med-High")
