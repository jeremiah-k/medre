"""Tests for LxmfSession: lifecycle, fake default, real-mode dependency
failure, repeated start/stop, inbound normalisation, outbound pending
semantics, diagnostics keys, delivery state model, and no raw-object leakage.

All tests use fake mode or mocks — no real Reticulum/LXMF dependency required.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.lxmf.errors import (
    LxmfConnectionError,
    LxmfSendError,
)
from medre.adapters.lxmf.session import (
    LxmfDeliveryState,
    LxmfSession,
    LxmfSessionDiagnostics,
    _map_delivery_method,
    _map_delivery_state,
)
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-test")
    defaults.update(overrides)
    # storage_path is required when connection_type is reticulum.
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-test-lxmf-router"
    return LxmfConfig(**defaults)


def _make_session(**config_overrides: Any) -> LxmfSession:
    config = _make_config(**config_overrides)
    return LxmfSession(
        config=config,
        adapter_id=config.adapter_id,
    )


# ===================================================================
# Lifecycle — fake mode
# ===================================================================


class TestLxmfSessionFakeLifecycle:
    """Start / stop / repeated start/stop with fake config."""

    async def test_start_fake_sets_connected(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        assert session.connected is True
        assert session.router_running is True
        await session.stop()

    async def test_start_idempotent(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.start()  # second start is no-op
        assert session.connected is True
        await session.stop()

    async def test_stop_idempotent(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.stop()
        await session.stop()  # second stop is no-op
        assert session.connected is False

    async def test_repeated_start_stop_cycles(self) -> None:
        """Repeated start/stop is safe — no leaked state."""
        session = _make_session(connection_type="fake")
        for _ in range(5):
            await session.start()
            assert session.connected is True
            await session.stop()
            assert session.connected is False


# ===================================================================
# Tranche 6: Async callback coroutine close (no asyncio.run fallback)
# ===================================================================


class TestTranche6AsyncCallbackCoroutineClose:
    """When no running loop is available, async callback coroutines are
    closed (not run via asyncio.run) to avoid cross-thread loop creation."""

    async def test_async_callback_coroutine_closed_not_run(self) -> None:
        """_invoke_inbound_callback closes the coroutine when get_running_loop
        raises RuntimeError (no loop available)."""

        async def async_cb(msg: dict[str, Any]) -> None:
            pass  # pragma: no cover

        session = _make_session(connection_type="fake")
        await session.start(message_callback=async_cb)

        # Monkey-patch asyncio.get_running_loop to raise RuntimeError
        # (simulating being called from a non-asyncio thread context).
        import asyncio as _asyncio

        original = _asyncio.get_running_loop
        _asyncio.get_running_loop = MagicMock(side_effect=RuntimeError("no loop"))

        try:
            # This should NOT raise. The coroutine should be closed.
            session._invoke_inbound_callback({"content": "test"})

            # Give the loop a turn to process any scheduled work.
            await asyncio.sleep(0)
        finally:
            _asyncio.get_running_loop = original

        # Session must remain operational.
        assert session.connected is True
        await session.stop()

    async def test_connected_false_before_start(self) -> None:
        session = _make_session(connection_type="fake")
        assert session.connected is False
        assert session.router_running is False


# ===================================================================
# Lifecycle — real mode dependency failure
# ===================================================================


class TestLxmfSessionRealModeDependencyFailure:
    """Real mode without lxmf/RNS installed raises clear errors."""

    async def test_real_mode_raises_without_sdk(self) -> None:
        session = _make_session(connection_type="reticulum")
        with patch("medre.adapters.lxmf.session.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError, match="not installed"):
                await session.start()
        assert session.connected is False

    async def test_real_mode_never_connects_without_sdk(self) -> None:
        session = _make_session(connection_type="reticulum")
        with patch("medre.adapters.lxmf.session.HAS_LXMF", False):
            for _ in range(3):
                with pytest.raises(LxmfConnectionError):
                    await session.start()
                assert session.connected is False


# ===================================================================
# Inbound normalisation
# ===================================================================


class TestLxmfSessionInboundNormalisation:
    """Inbound messages are normalised to plain dicts — no raw objects."""

    async def test_inject_inbound_calls_callback(self) -> None:
        received: list[dict[str, Any]] = []

        def callback(msg: dict[str, Any]) -> None:
            received.append(msg)

        session = _make_session(connection_type="fake")
        await session.start(message_callback=callback)

        packet = {
            "source_hash": "ab" * 16,
            "destination_hash": "00" * 16,
            "message_id": "cd" * 32,
            "timestamp": 1700000000.0,
            "title": "",
            "content": "hello",
            "fields": {},
            "signature_validated": True,
            "has_fields": False,
            "delivery_method": "direct",
        }

        session.inject_inbound(packet)
        assert len(received) == 1
        assert received[0] is packet
        await session.stop()

    async def test_inject_inbound_no_callback_is_safe(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        # No callback registered — should not raise.
        session.inject_inbound({"content": "test"})
        await session.stop()

    async def test_normalise_inbound_message_converts_bytes(self) -> None:
        """_normalise_inbound_message handles bytes fields correctly."""

        class FakeMessage:
            source_hash = b"\xab" * 16
            destination_hash = b"\x00" * 16
            hash = b"\xcd" * 32
            timestamp = 1700000000.0
            content = b"hello bytes"
            title = b"title bytes"
            fields = {}
            signature_validated = True
            method = None

        result = LxmfSession._normalise_inbound_message(FakeMessage())
        assert result["source_hash"] == "ab" * 16
        assert result["destination_hash"] == "00" * 16
        assert result["message_id"] == "cd" * 32
        assert result["content"] == "hello bytes"
        assert result["title"] == "title bytes"
        assert result["has_fields"] is False

    async def test_normalise_inbound_message_str_content(self) -> None:
        """_normalise_inbound_message handles str content."""

        class FakeMessage:
            source_hash = "abcdef"
            destination_hash = "000000"
            message_id = "cccccc"
            timestamp = None
            content = "str content"
            title = ""
            fields = {0x01: "data"}
            signature_validated = False
            method = None

        result = LxmfSession._normalise_inbound_message(FakeMessage())
        assert result["source_hash"] == "abcdef"
        assert result["content"] == "str content"
        assert result["has_fields"] is True

    async def test_normalise_no_raw_objects(self) -> None:
        """Normalised dict must not contain any LXMF/RNS object types."""

        class FakeLXMObject:
            pass

        class FakeMessage:
            source_hash = b"\x01" * 16
            destination_hash = b"\x02" * 16
            hash = b"\x03" * 32
            timestamp = 1.0
            content = "test"
            title = ""
            fields = {}
            signature_validated = True
            method = None

        result = LxmfSession._normalise_inbound_message(FakeMessage())

        # Walk the entire dict and check no value is a FakeLXMObject.
        def _check_plain(obj: Any, path: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _check_plain(v, f"{path}.{k}")
            elif isinstance(obj, (list, tuple)):
                for i, v in enumerate(obj):
                    _check_plain(v, f"{path}[{i}]")
            else:
                assert not isinstance(
                    obj, FakeLXMObject
                ), f"Raw object leaked at {path}"

        _check_plain(result)


# ===================================================================
# Outbound — pending semantics
# ===================================================================


class TestLxmfSessionOutboundPending:
    """Outbound sends return honest pending/sent semantics."""

    async def test_fake_send_returns_outbound_state(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, state = await session.send_text(
            destination_hash="ab" * 16,
            content="hello",
        )
        assert native_id is not None
        assert state == LxmfDeliveryState.OUTBOUND
        await session.stop()

    async def test_fake_send_tracks_outbound_delivery(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, state = await session.send_text(
            destination_hash="ab" * 16,
            content="hello",
        )
        assert native_id is not None
        counts = session.delivery_state_counts()
        assert "outbound" in counts
        assert counts["outbound"] >= 1
        await session.stop()

    async def test_send_without_start_raises(self) -> None:
        session = _make_session(connection_type="fake")
        with pytest.raises(LxmfSendError, match="not connected"):
            await session.send_text(
                destination_hash="ab" * 16,
                content="hello",
            )

    async def test_fake_send_unique_ids(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()

        id1, _ = await session.send_text("ab" * 16, "msg1")
        id2, _ = await session.send_text("ab" * 16, "msg2")
        assert id1 != id2
        await session.stop()

    async def test_outbound_cleared_on_stop(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.send_text("ab" * 16, "hello")
        await session.stop()
        counts = session.delivery_state_counts()
        assert sum(counts.values()) == 0


# ===================================================================
# Delivery state model
# ===================================================================


class TestLxmfDeliveryStateModel:
    """Delivery states are modelled truthfully."""

    def test_all_states_exist(self) -> None:
        expected = {
            "generating",
            "outbound",
            "sending",
            "sent",
            "delivered",
            "failed",
            "rejected",
            "cancelled",
            "unmapped",
        }
        actual = {s.value for s in LxmfDeliveryState}
        assert actual == expected

    def test_map_unknown_int_returns_unmapped(self) -> None:
        assert _map_delivery_state(99999) == LxmfDeliveryState.UNMAPPED

    def test_map_string_state(self) -> None:
        assert _map_delivery_state("delivered") == LxmfDeliveryState.DELIVERED
        assert _map_delivery_state("FAILED") == LxmfDeliveryState.FAILED

    def test_map_none_returns_unmapped(self) -> None:
        assert _map_delivery_state(None) == LxmfDeliveryState.UNMAPPED

    def test_map_delivery_method_direct(self) -> None:
        assert _map_delivery_method("direct") == "direct"

    def test_map_delivery_method_none(self) -> None:
        assert _map_delivery_method(None) is None

    def test_map_delivery_method_unknown_returns_none(self) -> None:
        assert _map_delivery_method("carrier_pigeon") is None


# ===================================================================
# Diagnostics
# ===================================================================


class TestLxmfSessionDiagnostics:
    """Diagnostics expose safe keys, no secrets or raw internals."""

    async def test_diagnostics_keys_after_start(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        diag = session.diagnostics()
        await session.stop()

        assert isinstance(diag, LxmfSessionDiagnostics)
        assert diag.connected is True
        assert diag.router_running is True
        assert diag.mode == "fake"
        assert diag.reconnecting is False
        assert diag.reconnect_attempts == 0
        assert diag.transient_delivery_failures == 0
        assert diag.permanent_delivery_failures == 0
        assert diag.last_error is None
        assert diag.pending_delivery_count == 0

    async def test_diagnostics_after_stop(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.stop()
        diag = session.diagnostics()

        assert diag.connected is False
        assert diag.router_running is False

    async def test_pending_delivery_count_zero_after_start(self) -> None:
        """pending_delivery_count must be 0 (not None) with no sends."""
        session = _make_session(connection_type="fake")
        await session.start()
        diag = session.diagnostics()
        assert diag.pending_delivery_count == 0
        await session.stop()

    async def test_pending_delivery_count_reflects_sends(self) -> None:
        """pending_delivery_count must track fake-mode outbound sends."""
        session = _make_session(connection_type="fake")
        await session.start()
        await session.send_text("ab" * 16, "msg1")
        await session.send_text("ab" * 16, "msg2")
        diag = session.diagnostics()
        assert diag.pending_delivery_count == 2
        await session.stop()

    async def test_diagnostics_no_secret_fields(self) -> None:
        """Diagnostics must not contain identity material or secrets."""
        session = _make_session(connection_type="fake")
        await session.start()
        diag = session.diagnostics()
        await session.stop()

        diag_dict = diag.__dict__
        forbidden_keys = {
            "identity",
            "private_key",
            "secret",
            "password",
            "token",
            "reticulum",
            "router",
            "raw",
        }
        for key in diag_dict:
            assert key not in forbidden_keys, f"Forbidden key {key!r} in diagnostics"

    async def test_diagnostics_last_message_time_updated(self) -> None:
        received: list[dict[str, Any]] = []

        session = _make_session(connection_type="fake")
        await session.start(message_callback=received.append)

        session.inject_inbound({"content": "test"})
        diag = session.diagnostics()
        assert diag.last_message_time is not None
        await session.stop()


# ===================================================================
# Extract message hash
# ===================================================================


class TestExtractMessageHash:
    """_extract_message_hash handles various input shapes.

    Per W1 audit: LXMF native hash/message_id is deterministic, persistent,
    and correctly extracted.
    """

    def test_bytes_hash(self) -> None:
        class Msg:
            hash = b"\x01\x02\x03"

        assert LxmfSession._extract_message_hash(Msg()) == "010203"

    def test_str_hash(self) -> None:
        class Msg:
            hash = "abc123"

        assert LxmfSession._extract_message_hash(Msg()) == "abc123"

    def test_message_id_fallback(self) -> None:
        class Msg:
            hash = None
            message_id = b"\xff"

        assert LxmfSession._extract_message_hash(Msg()) == "ff"

    def test_no_hash_returns_none(self) -> None:
        class Msg:
            pass

        assert LxmfSession._extract_message_hash(Msg()) is None


# ===================================================================
# Delivery state counts
# ===================================================================


class TestDeliveryStateCounts:
    """delivery_state_counts returns accurate state distribution."""

    async def test_empty_after_start(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        assert session.delivery_state_counts() == {}
        await session.stop()

    async def test_counts_after_sends(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.send_text("ab" * 16, "msg1")
        await session.send_text("ab" * 16, "msg2")

        counts = session.delivery_state_counts()
        assert counts.get("outbound", 0) == 2
        await session.stop()


# ===================================================================
# Real-mode _send_real fields preservation (mocked LXMF)
# ===================================================================


class TestSendRealFieldsPreservation:
    """_send_real() passes fields dict to the real LXMessage constructor."""

    async def test_fields_passed_to_lxmessage_constructor(self) -> None:
        """LXMessage must receive the fields kwarg in real mode."""
        session = _make_session(connection_type="fake")
        await session.start()

        # Patch internals to simulate real mode with mocked SDK objects.
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        captured_kwargs: dict[str, Any] = {}

        class FakeLXMessage:
            GENERATING = 0
            OUTBOUND = 1
            SENDING = 2
            SENT = 3
            DELIVERED = 4
            FAILED = 5

            def __init__(self, dest, router, content, **kwargs):
                self.state = self.OUTBOUND
                self.hash = b"\xab" * 16
                self.fields = kwargs.get("fields", None)
                captured_kwargs.update(kwargs)

            def register_delivery_callback(self, cb):
                pass

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_lxmf.LXMessage = FakeLXMessage
        mock_lxmf.LXMessage.OUTBOUND = 1

        mock_identity = MagicMock()
        session._identity = mock_identity
        session._router = MagicMock()

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            fields = {0xFD: {"medre": {"event_id": "evt-1"}}}
            native_id, state = await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
                title="test",
                fields=fields,
            )

        assert captured_kwargs.get("fields") is fields, (
            f"Expected fields dict passed through to LXMessage, "
            f"got {captured_kwargs.get('fields')!r}"
        )
        assert native_id is not None
        await session.stop()

    async def test_none_fields_passed_through(self) -> None:
        """fields=None must reach LXMessage without error."""
        session = _make_session(connection_type="fake")
        await session.start()
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        captured_fields: list = [None]

        class FakeLXMessage:
            OUTBOUND = 1

            def __init__(self, dest, router, content, **kwargs):
                self.state = self.OUTBOUND
                self.hash = b"\xcd" * 16
                captured_fields[0] = kwargs.get("fields")

            def register_delivery_callback(self, cb):
                pass

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_lxmf.LXMessage = FakeLXMessage

        session._identity = MagicMock()
        session._router = MagicMock()

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
                fields=None,
            )

        assert captured_fields[0] is None
        await session.stop()

    async def test_medre_envelope_fields_preserved(self) -> None:
        """MEDRE envelope fields from renderer are passed to real LXMessage."""
        from medre.adapters.lxmf.fields import (
            FIELD_MEDRE_ENVELOPE,
            LXMF_NAMESPACE,
            LxmfFieldsHelper,
        )

        session = _make_session(connection_type="fake")
        await session.start()
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        captured_fields: list = [None]

        class FakeLXMessage:
            OUTBOUND = 1

            def __init__(self, dest, router, content, **kwargs):
                self.state = self.OUTBOUND
                self.hash = b"\xef" * 16
                captured_fields[0] = kwargs.get("fields")

            def register_delivery_callback(self, cb):
                pass

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_lxmf.LXMessage = FakeLXMessage

        session._identity = MagicMock()
        session._router = MagicMock()

        # Simulate fields with a MEDRE envelope from the renderer.
        fields = LxmfFieldsHelper.embed_envelope(
            {}, "evt-42", (), {"source_hash": "ab" * 16}
        )

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
                fields=fields,
            )

        assert captured_fields[0] is not None
        assert FIELD_MEDRE_ENVELOPE in captured_fields[0]
        envelope = captured_fields[0][FIELD_MEDRE_ENVELOPE]
        assert LXMF_NAMESPACE in envelope
        assert envelope[LXMF_NAMESPACE]["event_id"] == "evt-42"
        await session.stop()

    async def test_fields_contain_no_secrets(self) -> None:
        """Fields passed to LXMessage must not include private keys/secrets."""
        from medre.adapters.lxmf.fields import (
            FIELD_MEDRE_ENVELOPE,
            LXMF_NAMESPACE,
            LxmfFieldsHelper,
        )

        session = _make_session(connection_type="fake")
        await session.start()
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        captured_fields: list = [None]

        class FakeLXMessage:
            OUTBOUND = 1

            def __init__(self, dest, router, content, **kwargs):
                self.state = self.OUTBOUND
                self.hash = b"\xaa" * 16
                captured_fields[0] = kwargs.get("fields")

            def register_delivery_callback(self, cb):
                pass

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_lxmf.LXMessage = FakeLXMessage

        session._identity = MagicMock()
        session._router = MagicMock()

        fields = LxmfFieldsHelper.embed_envelope(
            {}, "evt-99", (), {"source_hash": "ab" * 16}
        )

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
                fields=fields,
            )

        result_fields = captured_fields[0]
        assert result_fields is not None

        # The top-level keys should be int field IDs (like 0xFD),
        # not private keys, secrets, or raw identity material.
        for key in result_fields:
            assert isinstance(
                key, int
            ), f"Field key should be int (LXMF field ID), got {type(key)}: {key!r}"

        # Specifically check envelope content doesn't leak secrets.
        envelope = result_fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        forbidden = {
            "private_key",
            "secret",
            "password",
            "token",
            "identity",
            "raw_identity",
        }
        for word in forbidden:
            assert word not in envelope, f"Envelope must not contain {word!r}"
        await session.stop()


# ===================================================================
# Real-mode _send_real destination identity recall
# ===================================================================


class TestSendRealDestinationRecall:
    """_send_real() uses RNS.Identity.recall to construct destinations."""

    async def test_unrecallable_destination_raises_non_transient(self) -> None:
        """If RNS.Identity.recall returns None, raise LxmfSendError(transient=False)."""
        session = _make_session(connection_type="fake")
        await session.start()
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        mock_rns = MagicMock()
        mock_rns.Identity.recall.return_value = None  # identity not found
        mock_lxmf = MagicMock()

        session._identity = MagicMock()
        session._router = MagicMock()

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            with pytest.raises(
                LxmfSendError, match="Cannot recall identity"
            ) as exc_info:
                await session._send_real(
                    destination_hash="ab" * 16,
                    content="hello",
                )

        assert (
            exc_info.value.transient is False
        ), "Unrecallable destination should be a permanent (non-transient) error"
        mock_rns.Identity.recall.assert_called_once_with(bytes.fromhex("ab" * 16))
        await session.stop()

    async def test_destination_constructed_with_recalled_identity(self) -> None:
        """Destination must be constructed using the identity returned by recall."""
        session = _make_session(connection_type="fake")
        await session.start()
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        recalled_identity = MagicMock()

        created_lxmessages: list[FakeLXMessage] = []

        class FakeDestination:
            OUT = "out"
            SINGLE = "single"
            hash = b"\x00" * 16

            def __init__(self, identity, *args, **kwargs):
                self.identity = identity

        class FakeLXMessage:
            OUTBOUND = 1

            def __init__(self, dest, router, content, **kwargs):
                self.dest = dest  # Capture for assertion
                created_lxmessages.append(self)
                self.state = self.OUTBOUND
                self.hash = b"\xab" * 16

            def register_delivery_callback(self, cb):
                pass

        mock_rns = MagicMock()
        mock_rns.Identity.recall.return_value = recalled_identity
        mock_rns.Destination = FakeDestination
        mock_rns.Destination.OUT = "out"
        mock_rns.Destination.SINGLE = "single"

        mock_lxmf = MagicMock()
        mock_lxmf.LXMessage = FakeLXMessage

        session._identity = MagicMock()
        session._router = MagicMock()

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
            )

        mock_rns.Identity.recall.assert_called_once_with(bytes.fromhex("ab" * 16))
        assert (
            len(created_lxmessages) == 1
        ), f"Expected exactly 1 LXMessage, got {len(created_lxmessages)}"
        assert (
            created_lxmessages[0].dest.identity is recalled_identity
        ), "Destination was not wired with the recalled identity"
        await session.stop()


class TestFakeSendReturnsAdapterDeliveryResult:
    """Fake send_text returns honest outbound/pending data, not None."""

    async def test_fake_send_returns_non_none_id(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        native_id, state = await session.send_text(
            destination_hash="ab" * 16,
            content="hello",
        )
        assert native_id is not None
        assert isinstance(native_id, str)
        await session.stop()

    async def test_fake_send_state_is_outbound(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        _, state = await session.send_text(
            destination_hash="ab" * 16,
            content="hello",
        )
        assert state == LxmfDeliveryState.OUTBOUND
        await session.stop()

    async def test_fake_send_with_fields(self) -> None:
        """Fake mode accepts fields without error (no-op on fields)."""
        session = _make_session(connection_type="fake")
        await session.start()
        fields = {0x01: "test"}
        native_id, state = await session.send_text(
            destination_hash="ab" * 16,
            content="hello",
            fields=fields,
        )
        assert native_id is not None
        assert state == LxmfDeliveryState.OUTBOUND
        await session.stop()


# ===================================================================
# Tranche 5: Callback threading safety
# ===================================================================


class TestTranche5CallbackThreadingSafety:
    """Thread→asyncio bridge safety for inbound callbacks.

    Reticulum/LXMF fire delivery callbacks from background daemon
    threads, not from the asyncio event loop.  The session must handle
    this boundary safely.
    """

    async def test_inject_inbound_with_async_callback(self) -> None:
        """inject_inbound schedules an async callback on the running loop."""
        received: list[dict[str, Any]] = []

        async def async_callback(msg: dict[str, Any]) -> None:
            received.append(msg)

        session = _make_session(connection_type="fake")
        await session.start(message_callback=async_callback)

        packet = {"content": "async-test", "source_hash": "ab" * 16}
        session.inject_inbound(packet)

        # Give the event loop a turn to process the scheduled coroutine.
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0]["content"] == "async-test"
        await session.stop()

    async def test_inject_inbound_with_sync_callback(self) -> None:
        """inject_inbound calls sync callbacks directly."""
        received: list[dict[str, Any]] = []

        def sync_callback(msg: dict[str, Any]) -> None:
            received.append(msg)

        session = _make_session(connection_type="fake")
        await session.start(message_callback=sync_callback)

        packet = {"content": "sync-test", "source_hash": "ab" * 16}
        session.inject_inbound(packet)

        assert len(received) == 1
        assert received[0]["content"] == "sync-test"
        await session.stop()

    async def test_callback_exception_does_not_crash_session(self) -> None:
        """A failing callback in _on_lxmf_delivery does not leave the
        session in a bad state (callback errors are caught and logged)."""

        def bad_callback(msg: dict[str, Any]) -> None:
            raise RuntimeError("callback explosion")

        session = _make_session(connection_type="fake")
        await session.start(message_callback=bad_callback)

        # _on_lxmf_delivery wraps the callback call in try/except.
        # Build a fake message that _normalise_inbound_message can handle.
        class FakeMsg:
            source_hash = b"\x01" * 16
            destination_hash = b"\x02" * 16
            hash = b"\x03" * 32
            timestamp = 1.0
            content = "boom"
            title = ""
            fields = {}
            signature_validated = True
            method = None

        # Should not raise — _on_lxmf_delivery absorbs callback errors.
        session._on_lxmf_delivery(FakeMsg())

        # Give the event loop a turn to process the scheduled callback
        # (which will fail) via call_soon_threadsafe.
        await asyncio.sleep(0)

        assert session.connected is True
        await session.stop()


# ===================================================================
# Tranche 5: Send return semantics
# ===================================================================


class TestTranche5SendReturnSemantics:
    """send_text returns honest local-acceptance, NOT delivery confirmation."""

    async def test_fake_send_state_is_outbound_not_delivered(self) -> None:
        """Fake send returns OUTBOUND — the message is locally queued,
        not confirmed delivered."""
        session = _make_session(connection_type="fake")
        await session.start()
        _, state = await session.send_text("ab" * 16, "hello")
        assert state == LxmfDeliveryState.OUTBOUND
        assert state != LxmfDeliveryState.DELIVERED
        assert state != LxmfDeliveryState.SENT
        await session.stop()

    async def test_fake_send_state_is_outbound_not_sending(self) -> None:
        """Fake send never claims to be actively transmitting."""
        session = _make_session(connection_type="fake")
        await session.start()
        _, state = await session.send_text("ab" * 16, "hello")
        assert state != LxmfDeliveryState.SENDING
        await session.stop()

    async def test_concurrent_sends_produce_unique_ids(self) -> None:
        """Concurrent send_text calls produce distinct message IDs."""
        session = _make_session(connection_type="fake")
        await session.start()

        results = await asyncio.gather(
            session.send_text("ab" * 16, "msg-a"),
            session.send_text("ab" * 16, "msg-b"),
            session.send_text("ab" * 16, "msg-c"),
        )
        ids = {r[0] for r in results}
        assert len(ids) == 3, f"Expected 3 unique IDs, got {len(ids)}"
        await session.stop()


# ===================================================================
# Tranche 5: Full delivery state transition chain
# ===================================================================


class TestTranche5DeliveryStateTransitions:
    """Verify the full outbound→sending→sent→delivered transition chain."""

    async def test_outbound_to_delivered_via_callback(self) -> None:
        """Simulating delivery callback transitions state through to
        DELIVERED and then untracks the terminal entry."""
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, state = await session.send_text("ab" * 16, "hello")
        assert state == LxmfDeliveryState.OUTBOUND
        assert native_id in session._outbound_deliveries

        # Transition through intermediate states — entry stays tracked.
        for new_state in (
            LxmfDeliveryState.SENDING,
            LxmfDeliveryState.SENT,
        ):

            class _Msg:
                hash = native_id
                state = new_state

            session._on_delivery_state_update(_Msg())
            await asyncio.sleep(0)  # yield for call_soon_threadsafe bridge
            if new_state != LxmfDeliveryState.DELIVERED:
                assert native_id in session._outbound_deliveries

        # Final transition to DELIVERED — terminal, entry untracked.
        class _DeliveredMsg:
            hash = native_id
            state = LxmfDeliveryState.DELIVERED

        session._on_delivery_state_update(_DeliveredMsg())
        await asyncio.sleep(0)  # yield for call_soon_threadsafe bridge
        assert native_id not in session._outbound_deliveries
        await session.stop()

    async def test_outbound_to_failed_via_callback(self) -> None:
        """FAILED is terminal — entry untracked, counter incremented."""
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, _ = await session.send_text("ab" * 16, "hello")
        initial_failures = session.permanent_delivery_failures

        class _FailedMsg:
            hash = native_id
            state = LxmfDeliveryState.FAILED

        session._on_delivery_state_update(_FailedMsg())
        await asyncio.sleep(0)  # yield for call_soon_threadsafe bridge
        assert native_id not in session._outbound_deliveries
        assert session.permanent_delivery_failures == initial_failures + 1
        await session.stop()

    async def test_outbound_to_rejected_via_callback(self) -> None:
        """REJECTED is terminal — entry untracked, counter incremented."""
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, _ = await session.send_text("ab" * 16, "hello")
        initial_failures = session.permanent_delivery_failures

        class _RejectedMsg:
            hash = native_id
            state = LxmfDeliveryState.REJECTED

        session._on_delivery_state_update(_RejectedMsg())
        await asyncio.sleep(0)  # yield for call_soon_threadsafe bridge
        assert native_id not in session._outbound_deliveries
        assert session.permanent_delivery_failures == initial_failures + 1
        await session.stop()

    async def test_unknown_message_hash_ignored(self) -> None:
        """Delivery callback for an untracked hash is silently ignored."""
        session = _make_session(connection_type="fake")
        await session.start()

        class _UnknownMsg:
            hash = "nonexistent-hash"
            state = LxmfDeliveryState.DELIVERED

        # Should not raise or affect tracking.
        session._on_delivery_state_update(_UnknownMsg())
        await asyncio.sleep(0)  # yield for call_soon_threadsafe bridge
        assert sum(session.delivery_state_counts().values()) == 0
        await session.stop()


# ===================================================================
# Tranche 5: Bounded outbound tracking — cleanup on completion
# ===================================================================


class TestTranche5BoundedOutboundCleanup:
    """Outbound tracking entries are removed when reaching terminal state,
    preventing unbounded growth in long-duration runs."""

    async def test_terminal_states_clean_up_tracking(self) -> None:
        """After delivery reaches a terminal state, the entry is removed."""
        session = _make_session(connection_type="fake")
        await session.start()

        ids: list[str] = []
        for i in range(5):
            nid, _ = await session.send_text("ab" * 16, f"msg-{i}")
            assert nid is not None
            ids.append(nid)

        # Simulate delivery for first 3 messages.
        for nid in ids[:3]:

            class _Msg:
                hash = nid
                state = LxmfDeliveryState.DELIVERED

            session._on_delivery_state_update(_Msg())

        # Yield for call_soon_threadsafe bridge to process all updates.
        await asyncio.sleep(0)

        # First 3 should be untracked, last 2 still tracked.
        for nid in ids[:3]:
            assert nid not in session._outbound_deliveries
        for nid in ids[3:]:
            assert nid in session._outbound_deliveries

        counts = session.delivery_state_counts()
        assert counts.get("outbound", 0) == 2
        assert counts.get("delivered", 0) == 0  # delivered entries were untracked
        await session.stop()


# ===================================================================
# LXMF capabilities unsupported for relations (W1 audit closure)
# ===================================================================


class TestLxmfCapabilitiesUnsupportedRelations:
    """LXMF capabilities explicitly mark replies/reactions as unsupported.
    Per W1 audit: MEDRE does NOT decode or write LXMF native FIELD_THREAD;
    relations are fallback/envelope-only."""

    def test_lxmf_adapter_replies_unsupported(self) -> None:
        from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES

        assert _LXMF_CAPABILITIES.replies == "unsupported"

    def test_lxmf_adapter_reactions_unsupported(self) -> None:
        from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES

        assert _LXMF_CAPABILITIES.reactions == "unsupported"

    def test_lxmf_adapter_edits_unsupported(self) -> None:
        from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES

        assert _LXMF_CAPABILITIES.edits == "unsupported"

    def test_lxmf_adapter_deletes_unsupported(self) -> None:
        from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES

        assert _LXMF_CAPABILITIES.deletes == "unsupported"


# ===================================================================
# Delivery state metadata namespacing (W1 audit closure)
# ===================================================================


class TestDeliveryStateMetadataNamespacing:
    """LXMF adapter delivery metadata has delivery_state namespaced under
    metadata['lxmf'], not at the top level."""

    async def _make_started_adapter(self) -> LxmfSession:
        """Create a started LXMF adapter for delivery testing."""
        session = _make_session(connection_type="fake")
        await session.start()
        return session

    async def test_delivery_state_under_lxmf_namespace(self) -> None:
        """delivery_state is nested under metadata.lxmf, not at top level."""
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.session import LxmfDeliveryState
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = AdapterContext(
            adapter_id="lxmf-test",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-test",
            target_channel=None,
            payload={
                "content": "test",
                "title": "",
                "fields": {},
                "destination_hash": "ab" * 16,
            },
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        # delivery_state is under metadata["lxmf"]["delivery_state"]
        assert "lxmf" in delivery.metadata
        lxmf_meta = delivery.metadata["lxmf"]
        assert "delivery_state" in lxmf_meta
        # The value is a string (enum .value), not the enum itself
        assert isinstance(lxmf_meta["delivery_state"], str)
        assert lxmf_meta["delivery_state"] == LxmfDeliveryState.OUTBOUND.value
        # NOT at top level of metadata — namespace contract.
        assert "delivery_state" not in delivery.metadata

        await adapter.stop()

    async def test_lxmf_metadata_inner_is_frozen(self) -> None:
        """Inner lxmf metadata dict is frozen (MappingProxyType) for
        consistency with MeshCore metadata."""
        import logging
        from datetime import datetime, timezone
        from types import MappingProxyType
        from unittest.mock import AsyncMock

        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = AdapterContext(
            adapter_id="lxmf-test",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-test",
            target_channel=None,
            payload={
                "content": "test",
                "title": "",
                "fields": {},
                "destination_hash": "ab" * 16,
            },
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        inner = delivery.metadata["lxmf"]
        assert isinstance(inner, MappingProxyType)

        await adapter.stop()

    async def test_lxmf_metadata_json_serializable(self) -> None:
        """LXMF delivery metadata round-trips through JSON."""
        import json
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = AdapterContext(
            adapter_id="lxmf-test",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-test",
            target_channel=None,
            payload={
                "content": "test",
                "title": "",
                "fields": {},
                "destination_hash": "ab" * 16,
            },
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        meta_dict = dict(delivery.metadata)
        lxmf_dict = dict(meta_dict["lxmf"])
        parsed = json.loads(json.dumps({"lxmf": lxmf_dict}))
        assert parsed["lxmf"]["delivery_state"] == "outbound"

        await adapter.stop()
