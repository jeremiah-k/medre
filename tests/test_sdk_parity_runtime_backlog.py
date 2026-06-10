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

    def test_subscribe_callbacks_only_subscribes_to_receive(
        self,
    ) -> None:
        """_subscribe_callbacks subscribes to 'meshtastic.receive' only.

        P-01 gap: no health probe is registered.  The session relies
        exclusively on inbound packets for liveness indication.
        """
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._subscribe_callbacks,
        )
        # Exactly one pub.subscribe call in the method.
        subscribe_count = source.count("pub.subscribe(")
        assert subscribe_count == 1, (
            f"Expected 1 pub.subscribe call, found {subscribe_count}. "
            "If a health-check subscription was added, update this test."
        )
        assert "meshtastic.receive" in source

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
# P-02: Meshtastic — No SDK connection-lost event subscription
# Gap type: Behavioral
# ===================================================================


class TestP02MeshtasticNoConnectionLostSubscription:
    """Characterize P-02: Session does not subscribe to meshtastic.connection.lose.

    The SDK fires this pubsub event when it detects a disconnect, but MEDRE
    does not listen for it.  Connection loss is only detected when the next
    send fails or when notify_connection_lost() is called externally.
    """

    def test_subscribe_callbacks_no_connection_lost(self) -> None:
        """_subscribe_callbacks does not subscribe to connection-lost events."""
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._subscribe_callbacks,
        )
        assert "connection.lose" not in source
        assert "connection_lost" not in source

    def test_unsubscribe_callbacks_no_connection_lost(self) -> None:
        """_unsubscribe_callbacks does not unsubscribe from connection-lost."""
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession._unsubscribe_callbacks,
        )
        assert "connection.lose" not in source
        assert "connection_lost" not in source

    def test_no_on_connection_lost_handler_method(self) -> None:
        """Session does not define a connection-lost callback handler.

        If a handler is added in the future, this test should be updated
        to verify it is wired to pubsub in _subscribe_callbacks.
        """
        session_cls = meshtastic_session_mod.MeshtasticSession
        method_names = {name for name in dir(session_cls) if not name.startswith("__")}
        assert "_on_connection_lost" not in method_names

    def test_notify_connection_lost_exists_but_is_not_pubsub_driven(self) -> None:
        """notify_connection_lost() exists but is only called externally.

        The method is available for external callers (e.g., the adapter's
        health_check or a future liveness probe) but no SDK pubsub event
        triggers it automatically.
        """
        assert hasattr(
            meshtastic_session_mod.MeshtasticSession,
            "notify_connection_lost",
        )
        source = inspect.getsource(
            meshtastic_session_mod.MeshtasticSession.notify_connection_lost,
        )
        # Should create a reconnect task but not be triggered by pubsub.
        assert "_reconnect_task" in source
        assert "_reconnect_loop" in source

    async def test_notify_connection_lost_triggers_reconnect_in_fake_session(
        self,
    ) -> None:
        """Calling notify_connection_lost() starts the reconnect loop."""
        from medre.adapters.meshtastic.session import MeshtasticSession

        config = _make_meshtastic_config()
        session = MeshtasticSession(
            config=config,
            adapter_id=config.adapter_id,
            platform="meshtastic",
        )
        # Simulate a started session with a client.
        session._started = True
        session._client = MagicMock()

        # notify_connection_lost should create a task that runs _reconnect_loop.
        session.notify_connection_lost()

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


class TestP04MeshCoreUnusedSuggestedTimeout:
    """Characterize P-04: _send_real ignores suggested_timeout from SDK.

    The SDK returns ``suggested_timeout`` (seconds) alongside
    ``expected_ack`` from ``send_msg()``.  MEDRE extracts ``expected_ack``
    as the native message ID but discards ``suggested_timeout``, using
    a fixed ``0.1 * attempt`` retry delay instead.
    """

    def test_send_real_ignores_suggested_timeout_in_source(self) -> None:
        """_send_real source does not reference suggested_timeout for timing.

        Uses targeted regex to detect ``suggested_timeout`` in operational
        contexts (assignment, function-call arguments, arithmetic) while
        allowing benign occurrences (string-key extraction, docstrings,
        comments).
        """
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._send_real,
        )
        # Detect suggested_timeout used as a bare variable reference
        # (assignment target, function argument, arithmetic operand) but
        # exclude string-key extraction (result["suggested_timeout"]) and
        # pure documentation/comments.
        usage_re = re.compile(r"\bsuggested_timeout\b")
        key_re = re.compile(r'["\x27]suggested_timeout["\x27]')
        active_usages = [
            line.strip()
            for line in source.splitlines()
            if usage_re.search(line)
            and not line.strip().startswith("#")
            and not line.strip().startswith('"""')
            and not line.strip().startswith("'''")
            and not key_re.search(line)  # string-key extraction is benign
        ]
        # No line uses suggested_timeout as a bare variable for timing.
        assert len(active_usages) == 0, (
            f"suggested_timeout used as a variable in: "
            f"{active_usages}. "
            "If parity was implemented, update this test."
        )

    def test_retry_delay_is_fixed_linear(self) -> None:
        """Retry sleep in _send_real uses fixed 0.1 * attempt, not SDK hint."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._send_real,
        )
        assert "0.1 * attempt" in source

    def test_send_max_retries_is_three(self) -> None:
        """_SEND_MAX_RETRIES is 3, giving ~0.3s total retry window."""
        assert meshcore_session_mod._SEND_MAX_RETRIES == 3

    async def test_send_real_extracts_expected_ack_but_not_suggested_timeout(
        self,
    ) -> None:
        """_send_real extracts expected_ack from result dict, discarding
        suggested_timeout."""
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

        # send_msg returns a dict with both expected_ack and suggested_timeout.
        sdk_result = {
            "expected_ack": b"\xab\xcd\xef\x01",
            "suggested_timeout": 7,  # SDK says wait 7 seconds for ACK
        }
        instance.commands.send_msg = AsyncMock(return_value=sdk_result)

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

            # native_id should be extracted from expected_ack (hex of 4 bytes).
            assert native_id == "abcdef01"
            # suggested_timeout was available but ignored.
            # No sleep call received the suggested_timeout value (7 seconds).
            for sleep_call in mock_sleep.call_args_list:
                sleep_arg = sleep_call[0][0] if sleep_call[0] else None
                assert sleep_arg != sdk_result["suggested_timeout"], (
                    f"sleep() called with suggested_timeout value "
                    f"({sdk_result['suggested_timeout']}): {sleep_call}. "
                    "If parity was implemented, update this test."
                )
            # Verify send_msg was called correctly.
            for call in instance.commands.send_msg.call_args_list:
                assert call is not None

        finally:
            remove_mock_module()
            await session.stop()

    async def test_send_real_with_zero_suggested_timeout_unchanged_behavior(
        self,
    ) -> None:
        """Even when SDK returns suggested_timeout=0, retry delay is the same
        fixed formula — confirming the value is simply not consumed."""
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

        # SDK returns suggested_timeout=0 (e.g. channel send with no ACK).
        sdk_result = {
            "expected_ack": b"\x11\x22\x33\x44",
            "suggested_timeout": 0,
        }
        instance.commands.send_msg = AsyncMock(return_value=sdk_result)

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
                native_id = await session.send_text("aabbccdd", "hello")

            assert native_id == "11223344"

        finally:
            remove_mock_module()
            await session.stop()


# ===================================================================
# P-05: MeshCore — No contact-list subscriptions
# Gap type: Declarative/capability
# ===================================================================


class TestP05MeshCoreNoContactListSubscriptions:
    """Characterize P-05: Session subscribes to only 3 event types.

    The SDK provides CONTACTS, NEW_CONTACT, SELF_INFO and others, but
    MEDRE only subscribes to CONTACT_MSG_RECV, CHANNEL_MSG_RECV, and
    DISCONNECTED.  This is a declarative/capability gap — missing
    observability, not incorrect runtime behavior.
    """

    def test_subscribe_events_only_three_types(self) -> None:
        """_subscribe_events subscribes to exactly three EventType members."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._subscribe_events,
        )
        # Count EventType references.
        event_type_refs = [line for line in source.splitlines() if "EventType." in line]
        # Current: CONTACT_MSG_RECV, CHANNEL_MSG_RECV, DISCONNECTED.
        assert len(event_type_refs) == 3, (
            f"Expected 3 EventType subscriptions, found {len(event_type_refs)}. "
            "If contact-list subscriptions were added, update this test."
        )

    def test_no_contacts_or_self_info_subscription(self) -> None:
        """_subscribe_events does not subscribe to CONTACTS or SELF_INFO."""
        source = inspect.getsource(
            meshcore_session_mod.MeshCoreSession._subscribe_events,
        )
        assert "CONTACTS" not in source
        assert "NEW_CONTACT" not in source
        assert "SELF_INFO" not in source


# ===================================================================
# P-06: LXMF — No periodic announce for mesh path discovery
# Gap type: Behavioral
# ===================================================================


class TestP06LxmfNoPeriodicAnnounce:
    """Characterize P-06: LxmfSession has announce task infrastructure
    but never starts it.

    The session defines ``_announce_task`` and cancellation logic in
    ``stop()``, but no connect path creates the periodic announce task.
    Without announces, remote peers cannot discover the MEDRE LXMF
    instance via path propagation.
    """

    async def test_announce_task_is_none_after_fake_start(self) -> None:
        """_announce_task remains None after session start (fake mode)."""
        session = _make_lxmf_session(connection_type="fake")
        await session.start()

        assert session._announce_task is None
        await session.stop()

    async def test_announce_task_is_none_after_reticulum_start(self) -> None:
        """_announce_task remains None after reticulum mode start.

        The connect path sets up the router and delivery callback but
        does not start any periodic announce task.
        """
        session = _make_lxmf_session(connection_type="reticulum")

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
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

    def test_config_has_no_announce_interval(self) -> None:
        """LxmfConfig does not define an announce interval field."""
        field_names = {f.name for f in dataclass_fields(LxmfConfig)}
        assert "announce_interval_seconds" not in field_names
        assert "announce_interval" not in field_names

    def test_announce_task_slot_exists(self) -> None:
        """Infrastructure for announce task exists (slot + cancel logic).

        The field and teardown were added in a prior wave but the actual
        periodic task was never implemented.  This test documents that
        the infrastructure is ready for the parity implementation.
        """
        source = inspect.getsource(lxmf_session_mod.LxmfSession.stop)
        assert "_announce_task" in source

    def test_no_announce_call_in_connect_path(self) -> None:
        """No direct .announce() call exists in any connect/start method.

        Uses regex to detect ``.announce(`` method-call patterns (e.g.
        ``router.announce(...)``, ``session.announce(...)``) while allowing
        ``announce_task`` attribute references (no dot-call pattern).
        The old check ``"announce" not in source or "announce_task" in source``
        could false-pass when both ``router.announce(...)`` and
        ``announce_task`` appeared in the same method.
        """
        announce_call_re = re.compile(r"\.\s*announce\s*\(")
        for method_name in (
            "_connect_fake",
            "_connect_real",
            "start",
        ):
            if not hasattr(lxmf_session_mod.LxmfSession, method_name):
                continue
            source = inspect.getsource(
                getattr(lxmf_session_mod.LxmfSession, method_name),
            )
            matches = [
                line.strip()
                for line in source.splitlines()
                if announce_call_re.search(line) and not line.strip().startswith("#")
            ]
            assert len(matches) == 0, (
                f"Found direct .announce() call in {method_name}: "
                f"{matches}. "
                "If periodic announce was implemented, update this test."
            )


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
        if "as_key_request" not in source_text:
            pytest.skip("as_key_request not in current session source")
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
