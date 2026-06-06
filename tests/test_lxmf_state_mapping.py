"""Tests for LXMF delivery-state string mapping and _send_real retry paths.

Coverage targets:
- lxmf/session.py lines 244–248: _map_delivery_state string→enum (try/except)
- lxmf/session.py line 1220: retry loop entry
- lxmf/session.py lines 1291–1333: retry exhaustion paths:
  - Non-transient LxmfSendError → immediate re-raise
  - Transient LxmfSendError → record, sleep, continue, then exhaust
  - Generic Exception → record, sleep, continue, then exhaust

All tests use fake mode or mocks — no real Reticulum/LXMF dependency required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.lxmf.errors import LxmfSendError
from medre.adapters.lxmf.session import (
    LxmfDeliveryState,
    LxmfSession,
    _map_delivery_state,
)
from medre.config.adapters.lxmf import LxmfConfig

# ===================================================================
# Helpers
# ===================================================================


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-test")
    defaults.update(overrides)
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


class _FakeDestination:
    OUT = "out"
    SINGLE = "single"
    hash = b"\x00" * 16

    def __init__(self, identity: Any, *args: Any, **kwargs: Any) -> None:
        pass


class _FakeLXMessage:
    OUTBOUND = 1

    def __init__(self, dest: Any, router: Any, content: Any, **kwargs: Any) -> None:
        # Use string state so _map_delivery_state resolves via the
        # string-to-enum branch (HAS_LXMF is False in tests, so the
        # integer state-map is empty and an int would map to UNMAPPED).
        self.state = "outbound"
        self.hash = b"\xab" * 16

    def register_delivery_callback(self, cb: Any) -> None:
        pass


def _mock_rns_lxmf() -> tuple[MagicMock, MagicMock]:
    """Return (mock_rns, mock_lxmf) wired with _FakeDestination/_FakeLXMessage."""
    recalled_identity = MagicMock()
    mock_rns = MagicMock()
    mock_rns.Identity.recall.return_value = recalled_identity
    mock_rns.Destination = _FakeDestination
    mock_lxmf = MagicMock()
    mock_lxmf.LXMessage = _FakeLXMessage
    return mock_rns, mock_lxmf


async def _start_real_mocked_session(
    *,
    router_side_effect: Any = None,
) -> tuple[LxmfSession, MagicMock, MagicMock, MagicMock]:
    """Create a session in fake mode, switch to reticulum config, wire mocks.

    Returns (session, mock_rns, mock_lxmf, mock_router).
    """
    session = _make_session(connection_type="fake")
    await session.start()
    session._config = _make_config(connection_type="reticulum")
    session._diag.connected = True

    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_router = MagicMock()
    if router_side_effect is not None:
        mock_router.handle_outbound.side_effect = router_side_effect

    session._identity = MagicMock()
    session._router = mock_router
    return session, mock_rns, mock_lxmf, mock_router


# ===================================================================
# _map_delivery_state — string→enum path (lines 244–248)
# ===================================================================


class TestMapDeliveryStateStringToEnum:
    """Lines 244–248: string raw_state is lowered and converted to enum."""

    def test_valid_outbound(self) -> None:
        assert _map_delivery_state("outbound") == LxmfDeliveryState.OUTBOUND

    def test_valid_delivered(self) -> None:
        assert _map_delivery_state("delivered") == LxmfDeliveryState.DELIVERED

    def test_valid_failed(self) -> None:
        assert _map_delivery_state("failed") == LxmfDeliveryState.FAILED

    def test_valid_sending(self) -> None:
        assert _map_delivery_state("sending") == LxmfDeliveryState.SENDING

    def test_valid_sent(self) -> None:
        assert _map_delivery_state("sent") == LxmfDeliveryState.SENT

    def test_valid_rejected(self) -> None:
        assert _map_delivery_state("rejected") == LxmfDeliveryState.REJECTED

    def test_valid_cancelled(self) -> None:
        assert _map_delivery_state("cancelled") == LxmfDeliveryState.CANCELLED

    def test_valid_generating(self) -> None:
        assert _map_delivery_state("generating") == LxmfDeliveryState.GENERATING

    def test_uppercase_delivered(self) -> None:
        assert _map_delivery_state("DELIVERED") == LxmfDeliveryState.DELIVERED

    def test_mixed_case_failed(self) -> None:
        assert _map_delivery_state("Failed") == LxmfDeliveryState.FAILED

    def test_invalid_string_returns_unmapped(self) -> None:
        """Line 248: except ValueError → UNMAPPED for unknown strings."""
        assert _map_delivery_state("garbage") == LxmfDeliveryState.UNMAPPED

    def test_empty_string_returns_unmapped(self) -> None:
        assert _map_delivery_state("") == LxmfDeliveryState.UNMAPPED

    def test_partial_match_returns_unmapped(self) -> None:
        assert _map_delivery_state("deliver") == LxmfDeliveryState.UNMAPPED

    def test_numeric_string_returns_unmapped(self) -> None:
        assert _map_delivery_state("123") == LxmfDeliveryState.UNMAPPED


# ===================================================================
# _send_real retry loop — line 1220 (loop entry)
# ===================================================================


class TestRetryLoopEntry:
    """Line 1220: the retry for-loop is entered when _send_real is called."""

    async def test_first_attempt_succeeds_enters_loop(self) -> None:
        """Successful send on first attempt enters the loop and returns."""
        session, mock_rns, mock_lxmf, mock_router = await _start_real_mocked_session()

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            native_id, state = await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
            )

        assert native_id is not None
        assert state == LxmfDeliveryState.OUTBOUND
        mock_router.handle_outbound.assert_called_once()
        await session.stop()


# ===================================================================
# _send_real retry — non-transient LxmfSendError (lines 1291–1295)
# ===================================================================


class TestNonTransientLxmfSendError:
    """Lines 1291–1295: non-transient LxmfSendError re-raises immediately."""

    async def test_non_transient_reraises_without_retry(self) -> None:
        session, mock_rns, mock_lxmf, mock_router = await _start_real_mocked_session(
            router_side_effect=LxmfSendError("permanent", transient=False),
        )

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            with pytest.raises(LxmfSendError, match="permanent") as exc_info:
                await session._send_real(
                    destination_hash="ab" * 16,
                    content="hello",
                )

        assert exc_info.value.transient is False
        # No retries — called exactly once.
        assert mock_router.handle_outbound.call_count == 1
        assert session._diag.permanent_delivery_failures == 1
        assert session._diag.transient_delivery_failures == 0
        await session.stop()


# ===================================================================
# _send_real retry — transient LxmfSendError (lines 1296–1309, 1326–1333)
# ===================================================================


class TestTransientLxmfSendErrorRetry:
    """Lines 1296–1309: transient LxmfSendError records failure and retries.
    Lines 1326–1333: exhaustion after all retries."""

    async def test_transient_exhausts_all_retries(self) -> None:
        """All 3 attempts fail with transient LxmfSendError → exhaustion."""
        session, mock_rns, mock_lxmf, mock_router = await _start_real_mocked_session(
            router_side_effect=LxmfSendError("temp glitch", transient=True),
        )

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            with pytest.raises(
                LxmfSendError, match=r"Send failed after 3 attempts"
            ) as exc_info:
                await session._send_real(
                    destination_hash="ab" * 16,
                    content="hello",
                )

        assert exc_info.value.transient is True
        # 3 transient in loop (exhaustion is a summary, not a separate increment)
        assert session._diag.transient_delivery_failures == 3
        assert session._diag.permanent_delivery_failures == 0
        assert mock_router.handle_outbound.call_count == 3
        await session.stop()

    async def test_transient_failure_then_success(self) -> None:
        """First attempt raises transient LxmfSendError, second succeeds."""
        session, mock_rns, mock_lxmf, mock_router = await _start_real_mocked_session()
        call_count = 0

        def _handle(msg: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise LxmfSendError("first try", transient=True)

        mock_router.handle_outbound.side_effect = _handle

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            native_id, state = await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
            )

        assert native_id is not None
        assert mock_router.handle_outbound.call_count == 2
        assert session._diag.transient_delivery_failures == 1
        assert session._diag.permanent_delivery_failures == 0
        await session.stop()


# ===================================================================
# _send_real retry — generic Exception (lines 1310–1324, 1326–1333)
# ===================================================================


class TestGenericExceptionRetry:
    """Lines 1310–1324: generic Exception records failure and retries.
    Lines 1326–1333: exhaustion after all retries."""

    async def test_generic_exception_exhausts_retries(self) -> None:
        """All 3 attempts fail with RuntimeError → exhaustion raises
        LxmfSendError(transient=True)."""
        session, mock_rns, mock_lxmf, mock_router = await _start_real_mocked_session(
            router_side_effect=RuntimeError("transport error"),
        )

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            with pytest.raises(
                LxmfSendError, match=r"Send failed after 3 attempts"
            ) as exc_info:
                await session._send_real(
                    destination_hash="ab" * 16,
                    content="hello",
                )

        assert exc_info.value.transient is True
        # 3 generic failures in loop (exhaustion is a summary, not a separate increment)
        assert session._diag.transient_delivery_failures == 3
        assert session._diag.permanent_delivery_failures == 0
        assert mock_router.handle_outbound.call_count == 3
        await session.stop()

    async def test_generic_failure_then_success(self) -> None:
        """First attempt raises RuntimeError, second succeeds."""
        session, mock_rns, mock_lxmf, mock_router = await _start_real_mocked_session()
        call_count = 0

        def _handle(msg: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first try failed")

        mock_router.handle_outbound.side_effect = _handle

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            native_id, state = await session._send_real(
                destination_hash="ab" * 16,
                content="hello",
            )

        assert native_id is not None
        assert mock_router.handle_outbound.call_count == 2
        assert session._diag.transient_delivery_failures == 1
        assert session._diag.permanent_delivery_failures == 0
        await session.stop()


# ===================================================================
# LXMF codec native hash persistence (W1 audit closure)
# ===================================================================


class TestLxmfCodecNativeHashPersistence:
    """LXMF codec persists message_id as source_native_ref.native_message_id.
    Per W1 audit: message_id is deterministic SHA-256, always computable
    from message content."""

    def test_codec_sets_source_native_ref_from_packet_id(self) -> None:
        """Codec creates source_native_ref with native_message_id from
        the classifier's packet_id (the message hash)."""
        from medre.adapters.lxmf.codec import LxmfCodec

        config = _make_config()
        codec = LxmfCodec(config.adapter_id, config)
        packet = {
            "source_hash": "ab" * 16,
            "destination_hash": "00" * 16,
            "message_id": "cd" * 32,
            "timestamp": 1700000000.0,
            "content": "hello",
            "title": "",
            "fields": {},
            "signature_validated": True,
            "has_fields": False,
            "delivery_method": "direct",
        }
        event = codec.decode(packet)
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "cd" * 32
        assert event.source_native_ref.adapter == "lxmf-test"
        assert event.source_native_ref.native_channel_id is None

    def test_codec_empty_relations_envelope_only(self) -> None:
        """Codec produces empty relations — MEDRE does not decode LXMF
        native FIELD_THREAD.  Relations are envelope-only (0xFD)."""
        from medre.adapters.lxmf.codec import LxmfCodec

        config = _make_config()
        codec = LxmfCodec(config.adapter_id, config)
        packet = {
            "source_hash": "ab" * 16,
            "destination_hash": "00" * 16,
            "message_id": "cd" * 32,
            "timestamp": 1700000000.0,
            "content": "hello",
            "title": "",
            "fields": {},
            "signature_validated": True,
            "has_fields": False,
            "delivery_method": "direct",
        }
        event = codec.decode(packet)
        assert event.relations == ()

    def test_codec_no_packet_id_produces_no_native_ref(self) -> None:
        """When message_id is None, codec produces no source_native_ref."""
        from medre.adapters.lxmf.codec import LxmfCodec

        config = _make_config()
        codec = LxmfCodec(config.adapter_id, config)
        packet = {
            "source_hash": "ab" * 16,
            "destination_hash": "00" * 16,
            "message_id": None,
            "timestamp": 1700000000.0,
            "content": "hello",
            "title": "",
            "fields": {},
            "signature_validated": True,
            "has_fields": False,
            "delivery_method": "direct",
        }
        event = codec.decode(packet)
        assert event.source_native_ref is None


# ===================================================================
# LXMF fallback no-duplication (W1 audit closure)
# ===================================================================


class TestLxmfFallbackNoDuplication:
    """Under fallback_text, the LXMF renderer embeds relations=() in the
    envelope and adds inline text — no duplication between structured
    envelope and inline fallback."""

    async def test_fallback_envelope_has_empty_relations(self) -> None:
        """Under fallback_text, the MEDRE envelope carries empty relations
        to avoid duplicating relation data as both structured and inline."""
        from medre.adapters.lxmf.fields import LxmfFieldsHelper
        from medre.adapters.lxmf.renderer import LxmfRenderer
        from medre.core.events import EventRelation
        from tests.helpers.rendering_evidence import make_context, make_event

        renderer = LxmfRenderer(metadata_embedding=True)
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target",
            target_native_ref=None,
            key=None,
            fallback_text="original message",
        )
        event = make_event(
            payload={"text": "reply body"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"

        fields = result.payload.get("fields", {})
        assert isinstance(fields, dict)
        envelope = LxmfFieldsHelper.extract_envelope(fields)
        assert envelope is not None
        # Envelope has empty relations — inline text is the only representation
        assert envelope["relations"] == []

        # But inline text contains the relation
        content = result.payload.get("content", "")
        assert isinstance(content, str)
        assert "reply" in content.lower()

    async def test_normal_mode_envelope_has_structured_relations(self) -> None:
        """Under normal delivery_strategy, envelope carries full relations."""
        from medre.adapters.lxmf.fields import LxmfFieldsHelper
        from medre.adapters.lxmf.renderer import LxmfRenderer
        from medre.core.events import EventRelation
        from tests.helpers.rendering_evidence import make_context, make_event

        renderer = LxmfRenderer(metadata_embedding=True)
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target",
            target_native_ref=None,
            key=None,
            fallback_text="original message",
        )
        event = make_event(
            payload={"text": "reply body"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="direct",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied is None

        fields = result.payload.get("fields", {})
        assert isinstance(fields, dict)
        envelope = LxmfFieldsHelper.extract_envelope(fields)
        assert envelope is not None
        # Envelope has structured relations
        assert len(envelope["relations"]) == 1
        assert envelope["relations"][0]["relation_type"] == "reply"

        # Content does NOT contain inline relation text
        content = result.payload.get("content", "")
        assert isinstance(content, str)
        assert "[reply" not in content
