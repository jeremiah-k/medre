"""Matrix native lifecycle: edits, redactions, threads, closed envelope.

Covers slice B of the relation-binding contract:

* renderer edit shape (``m.replace`` / ``m.new_content`` / exactly-one
  attribution / bound original-copy target);
* renderer fail-close for mutations without a ``bound_owned`` fact;
* redaction rendering (``redact_event``) and adapter dispatch through
  ``MatrixSession.room_redact``;
* thread root/parent/``is_falling_back`` semantics, reply-in-thread,
  and unbound-root degradation;
* strict adapter envelope dispatch (missing/invalid => permanent error,
  nothing leaks to homeserver content);
* deterministic redaction transaction identity (never shared with a
  send or another target) and 429 retry-after ownership;
* the pinned mindroom-nio ``room_redact`` SDK contract (real response
  classes; ``matrix_sdk`` marker);
* capability interaction: plain thread events degrade to
  ``fallback_text`` on replies-unsupported transports while explicit
  reply-in-thread follows plain-reply semantics.

These tests construct relations with the core-computed
``RelationTargetFact`` from the relation-binding contract.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import (
    MatrixAdapter,
    _matrix_redact_txn_id,
    _matrix_txn_id,
    _NioRateLimitError,
)
from medre.adapters.matrix.outbound import (
    MATRIX_OPERATION_KEY,
    MatrixOutboundEnvelopeError,
    MatrixOutboundOperation,
)
from medre.adapters.matrix.renderer import MatrixNativeMutationError, MatrixRenderer
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeRef,
    RelationTargetFact,
)
from medre.core.events.metadata import EventMetadata
from medre.core.planning.capability_decision import resolver
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from tests.helpers.matrix_adapter import wire_mock_session as _wire_mock_session
from tests.helpers.pipeline import make_event

_ADAPTER = "matrix-1"
_ROOM = "!room:server"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact(
    status: str = "bound_owned",
    native_message_id: str = "$orig-copy",
    adapter: str = _ADAPTER,
    reason: str = "authorship proven",
) -> RelationTargetFact:
    return RelationTargetFact(
        status=status,
        adapter=adapter,
        native_channel_id=_ROOM,
        native_message_id=native_message_id,
        native_thread_id=None,
        direction="outbound" if status == "bound_owned" else None,
        reason=reason,
    )


def _rel(
    relation_type: str,
    *,
    native_id: str | None = None,
    fact: RelationTargetFact | None = None,
    key: str | None = None,
) -> EventRelation:
    return EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=None,
        target_native_ref=(
            NativeRef(
                adapter=_ADAPTER,
                native_channel_id=_ROOM,
                native_message_id=native_id,
            )
            if native_id
            else None
        ),
        key=key,
        fallback_text=None,
        target_fact=fact,
    )


def _lifecycle_event(
    relations: tuple[EventRelation, ...],
    *,
    body: str = "edited text",
    kind: str = "message.edited",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-1",
        event_kind=kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="mesh-1",
        source_transport_id="sender-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"body": body},
        metadata=EventMetadata(),
    )


def _direct_ctx(target_adapter: str = _ADAPTER) -> RenderingContext:
    return RenderingContext(target_adapter=target_adapter, delivery_strategy="direct")


def _op_of(result: RenderingResult) -> MatrixOutboundOperation:
    """Parse the closed envelope from a render result payload."""
    operation = MatrixOutboundOperation.from_payload(result.payload)
    assert operation is not None
    return operation


def _content_of(result: RenderingResult) -> dict[str, object]:
    operation = _op_of(result)
    assert operation.content is not None
    return operation.content


def _make_adapter_config(**overrides: Any) -> MatrixConfig:
    defaults: dict[str, Any] = {
        "adapter_id": _ADAPTER,
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_adapter(session_client: MagicMock | None = None) -> MatrixAdapter:
    adapter = MatrixAdapter(_make_adapter_config())
    adapter.ctx = AdapterContext(
        adapter_id=_ADAPTER,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.matrix-native-lifecycle"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
    _wire_mock_session(adapter, session_client or MagicMock())
    return adapter


def _envelope_result(
    operation: MatrixOutboundOperation,
    *,
    event_id: str = "evt-1",
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter=_ADAPTER,
        target_channel=_ROOM,
        payload=operation.to_payload(),
    )


# ---------------------------------------------------------------------------
# Renderer: native edits
# ---------------------------------------------------------------------------


class TestRendererEdits:
    async def test_edit_renders_replace_with_new_content(self) -> None:
        renderer = MatrixRenderer()
        edit_rel = _rel("edit", native_id="$orig", fact=_fact())
        result = await renderer.render(_lifecycle_event((edit_rel,)), _direct_ctx())

        operation = _op_of(result)
        assert operation.kind == "send_event"
        assert operation.event_type == "m.room.message"
        content = _content_of(result)

        # Spec fallback body carries the "* " prefix; m.new_content the real body.
        assert content["body"] == "* edited text"
        new_content = content["m.new_content"]
        assert new_content["body"] == "edited text"
        assert new_content["msgtype"] == "m.text"
        assert new_content["formatted_body"] == "<p>edited text</p>"
        # Target is the bound ORIGINAL copy native id.
        assert content["m.relates_to"] == {
            "rel_type": "m.replace",
            "event_id": "$orig-copy",
        }
        # Escaped HTML in the fallback formatted body.
        assert content["formatted_body"] == "<p>* edited text</p>"

    async def test_edit_applies_relay_prefix_exactly_once(self) -> None:
        renderer = MatrixRenderer(
            configs={
                _ADAPTER: SimpleNamespace(relay_prefix="[mesh] "),
            }
        )
        edit_rel = _rel("edit", native_id="$orig", fact=_fact())
        result = await renderer.render(
            _lifecycle_event((edit_rel,), body="hello"), _direct_ctx()
        )
        content = _content_of(result)
        assert content["body"] == "* [mesh] hello"
        assert content["m.new_content"]["body"] == "[mesh] hello"
        # Exactly one attribution in each body.
        assert str(content["body"]).count("[mesh]") == 1
        assert str(content["m.new_content"]["body"]).count("[mesh]") == 1

    async def test_edit_mirrors_bound_reply_into_new_content(self) -> None:
        renderer = MatrixRenderer()
        edit_rel = _rel("edit", native_id="$orig", fact=_fact())
        reply_rel = _rel(
            "reply", native_id="$parent", fact=_fact(native_message_id="$parent")
        )
        result = await renderer.render(
            _lifecycle_event((edit_rel, reply_rel)), _direct_ctx()
        )
        new_content = _content_of(result)["m.new_content"]
        assert new_content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$parent"}}
        # Top-level relates_to stays the edit relation.
        assert _content_of(result)["m.relates_to"]["rel_type"] == "m.replace"

    async def test_edit_without_bound_owned_fact_fails_closed(self) -> None:
        renderer = MatrixRenderer()
        for fact in (
            None,
            _fact(status="bound", native_message_id="$x"),
            _fact(status="not_authorized", native_message_id="$x"),
        ):
            edit_rel = _rel("edit", native_id="$orig", fact=fact)
            with pytest.raises(MatrixNativeMutationError):
                await renderer.render(_lifecycle_event((edit_rel,)), _direct_ctx())

    async def test_edit_bound_owned_fact_for_other_adapter_fails_closed(self) -> None:
        renderer = MatrixRenderer()
        edit_rel = _rel(
            "edit",
            native_id="$orig",
            fact=_fact(native_message_id="$elsewhere", adapter="matrix-2"),
        )
        with pytest.raises(MatrixNativeMutationError):
            await renderer.render(_lifecycle_event((edit_rel,)), _direct_ctx())


# ---------------------------------------------------------------------------
# Renderer: native deletes (redactions)
# ---------------------------------------------------------------------------


class TestRendererDeletes:
    async def test_delete_renders_redact_event_with_neutral_reason(self) -> None:
        renderer = MatrixRenderer()
        delete_rel = _rel(
            "delete", native_id="$owned", fact=_fact(native_message_id="$owned")
        )
        result = await renderer.render(
            _lifecycle_event((delete_rel,), kind="message.deleted", body="irrelevant"),
            _direct_ctx(),
        )
        operation = _op_of(result)
        assert operation.kind == "redact_event"
        assert operation.redacts_event_id == "$owned"
        assert operation.reason == "Deleted by original author via MEDRE relay"
        assert operation.content is None
        # No fabricated message body anywhere in the payload.
        assert "msgtype" not in result.payload
        assert "body" not in result.payload

    async def test_delete_without_bound_owned_fact_fails_closed(self) -> None:
        renderer = MatrixRenderer()
        for fact in (
            None,
            _fact(status="ambiguous", native_message_id="$x"),
            _fact(status="binding_unavailable", native_message_id=None),
        ):
            delete_rel = _rel("delete", native_id="$owned", fact=fact)
            with pytest.raises(MatrixNativeMutationError):
                await renderer.render(
                    _lifecycle_event((delete_rel,), kind="message.deleted"),
                    _direct_ctx(),
                )


# ---------------------------------------------------------------------------
# Renderer: native threads
# ---------------------------------------------------------------------------


class TestRendererThreads:
    async def test_thread_without_explicit_reply_falls_back_to_root(self) -> None:
        renderer = MatrixRenderer()
        thread_rel = _rel("thread", native_id="$thread-root")
        result = await renderer.render(
            _lifecycle_event((thread_rel,), kind="message.created", body="in thread"),
            _direct_ctx(),
        )
        content = _content_of(result)
        assert content["m.relates_to"] == {
            "rel_type": "m.thread",
            "event_id": "$thread-root",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$thread-root"},
        }

    async def test_thread_with_explicit_reply_is_order_independent(self) -> None:
        renderer = MatrixRenderer()
        thread_rel = _rel("thread", native_id="$thread-root")
        reply_rel = _rel("reply", native_id="$latest")
        for relations in ((thread_rel, reply_rel), (reply_rel, thread_rel)):
            result = await renderer.render(
                _lifecycle_event(
                    relations, kind="message.created", body="reply in thread"
                ),
                _direct_ctx(),
            )
            content = _content_of(result)
            assert content["m.relates_to"] == {
                "rel_type": "m.thread",
                "event_id": "$thread-root",
                "is_falling_back": False,
                "m.in_reply_to": {"event_id": "$latest"},
            }

    async def test_thread_with_unbound_root_degrades_to_plain_message(self) -> None:
        renderer = MatrixRenderer()
        thread_rel = _rel("thread", native_id=None)
        result = await renderer.render(
            _lifecycle_event((thread_rel,), kind="message.created"), _direct_ctx()
        )
        content = _content_of(result)
        assert "m.relates_to" not in content
        assert content["msgtype"] == "m.text"
        assert content["body"] == "edited text"


# ---------------------------------------------------------------------------
# Adapter: strict closed-envelope dispatch
# ---------------------------------------------------------------------------


class TestAdapterEnvelopeDispatch:
    async def test_missing_envelope_is_permanent_error(self) -> None:
        adapter = _make_adapter()
        result = RenderingResult(
            event_id="evt-1",
            target_adapter=_ADAPTER,
            target_channel=_ROOM,
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="_matrix_operation"):
            await adapter.deliver(result)

    async def test_malformed_envelope_is_permanent_error(self) -> None:
        adapter = _make_adapter()
        for bad in (
            {MATRIX_OPERATION_KEY: {"kind": "teleport"}},
            {MATRIX_OPERATION_KEY: {"kind": "send_event"}},
            {MATRIX_OPERATION_KEY: {"kind": "send_event", "bogus": True}},
            {MATRIX_OPERATION_KEY: {"kind": "redact_event", "content": {}}},
        ):
            result = RenderingResult(
                event_id="evt-1",
                target_adapter=_ADAPTER,
                target_channel=_ROOM,
                payload=bad,
            )
            with pytest.raises(AdapterPermanentError):
                await adapter.deliver(result)

    async def test_send_event_dispatch_keeps_existing_send_path(self) -> None:
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=SimpleNamespace(event_id="$sent-1")
        )
        mock_client.room_redact = AsyncMock()
        adapter = _make_adapter(mock_client)

        operation = MatrixOutboundOperation.send_event(
            "m.room.message", {"msgtype": "m.text", "body": "hello"}
        )
        delivery = await adapter.deliver(_envelope_result(operation))

        assert delivery.native_message_id == "$sent-1"
        assert delivery.native_channel_id == _ROOM
        sent_kwargs = mock_client.room_send.call_args.kwargs
        assert sent_kwargs["message_type"] == "m.room.message"
        # Nothing under the envelope key ever reaches the homeserver.
        assert MATRIX_OPERATION_KEY not in sent_kwargs["content"]
        assert sent_kwargs["content"] == {"msgtype": "m.text", "body": "hello"}
        mock_client.room_redact.assert_not_called()

    async def test_redact_event_dispatch_records_own_native_id(self) -> None:
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock()
        mock_client.room_redact = AsyncMock(
            return_value=SimpleNamespace(event_id="$redaction-1", room_id=_ROOM)
        )
        adapter = _make_adapter(mock_client)

        operation = MatrixOutboundOperation.redact("$owned-copy")
        delivery = await adapter.deliver(_envelope_result(operation))

        # The redaction's native ref records to its OWN canonical
        # mutation event — the redaction event's own id.
        assert delivery.native_message_id == "$redaction-1"
        assert delivery.native_channel_id == _ROOM
        redact_kwargs = mock_client.room_redact.call_args.kwargs
        assert redact_kwargs["event_id"] == "$owned-copy"
        assert redact_kwargs["reason"] == "Deleted by original author via MEDRE relay"
        assert redact_kwargs["room_id"] == _ROOM
        mock_client.room_send.assert_not_called()

    async def test_redaction_txn_is_deterministic_and_never_shared(self) -> None:
        result = RenderingResult(
            event_id="evt-1",
            target_adapter=_ADAPTER,
            target_channel=_ROOM,
            payload={},
        )
        redact_a = _matrix_redact_txn_id(result, _ROOM, "$owned-copy")
        redact_b = _matrix_redact_txn_id(result, _ROOM, "$owned-copy")
        assert redact_a == redact_b

        # Different target → different txn.
        redact_other = _matrix_redact_txn_id(result, _ROOM, "$other-copy")
        assert redact_other != redact_a

        # Never shares a txn with a send of the same rendering result.
        send_txn = _matrix_txn_id(result, _ROOM)
        assert redact_a != send_txn

    async def test_redaction_txn_stable_across_retries(self) -> None:
        mock_client = MagicMock()
        txns_seen: list[str] = []
        calls = 0

        async def _flaky(**kwargs: Any) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            txns_seen.append(kwargs["tx_id"])
            if calls <= 2:
                raise ConnectionError("network glitch")
            return SimpleNamespace(event_id="$redaction-1", room_id=_ROOM)

        mock_client.room_redact = AsyncMock(side_effect=_flaky)
        adapter = _make_adapter(mock_client)

        operation = MatrixOutboundOperation.redact("$owned-copy")
        delivery = await adapter.deliver(_envelope_result(operation))
        assert delivery.native_message_id == "$redaction-1"
        assert calls == 3
        assert len(set(txns_seen)) == 1
        assert txns_seen[0] == _matrix_redact_txn_id(
            _envelope_result(operation), _ROOM, "$owned-copy"
        )


# ---------------------------------------------------------------------------
# Adapter: redaction rate-limit ownership and error classification
# ---------------------------------------------------------------------------


class TestRedactionRateLimitOwnership:
    async def test_redaction_429_is_transient_with_retry_hint(self) -> None:
        mock_client = MagicMock()

        async def _rate_limited(**kwargs: Any) -> None:
            raise _NioRateLimitError("M_LIMIT_EXCEEDED", retry_after_ms=1500)

        mock_client.room_redact = AsyncMock(side_effect=_rate_limited)
        adapter = _make_adapter(mock_client)

        operation = MatrixOutboundOperation.redact("$owned-copy")
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_envelope_result(operation))

        assert exc_info.value.transient is True
        assert exc_info.value.retry_after_seconds == 1.5
        assert "rate-limited" in str(exc_info.value)
        # The server-directed window also extends the shared cooldown.
        assert adapter._outbound_rate_limit_events == 1
        assert adapter._outbound_cooldown_remaining() > 0

    async def test_redaction_forbidden_is_permanent(self) -> None:
        mock_client = MagicMock()
        mock_client.room_redact = AsyncMock(
            return_value=SimpleNamespace(
                status_code="M_FORBIDDEN", errcode="M_FORBIDDEN"
            )
        )
        adapter = _make_adapter(mock_client)

        operation = MatrixOutboundOperation.redact("$owned-copy")
        with pytest.raises(AdapterPermanentError):
            await adapter.deliver(_envelope_result(operation))

    async def test_redaction_honors_shared_cooldown_gate(self) -> None:
        mock_client = MagicMock()
        mock_client.room_redact = AsyncMock(
            return_value=SimpleNamespace(event_id="$redaction-1", room_id=_ROOM)
        )
        adapter = _make_adapter(mock_client)
        # A sibling delivery already recorded a homeserver cooldown.
        adapter._remember_outbound_cooldown(120.0)

        operation = MatrixOutboundOperation.redact("$owned-copy")
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_envelope_result(operation))

        assert exc_info.value.transient is True
        assert exc_info.value.retry_after_seconds is not None
        # Fail-fast: no transport call happened.
        mock_client.room_redact.assert_not_called()


# ---------------------------------------------------------------------------
# Session: room_redact boundary and rate-limit interception
# ---------------------------------------------------------------------------


class TestSessionRoomRedact:
    async def test_session_registers_redact_rate_limit_interceptor(
        self, mock_nio
    ) -> None:
        session = MatrixSession(
            MatrixConfig(
                adapter_id="matrix-1",
                homeserver="https://matrix.example.com",
                user_id="@bot:example.com",
                access_token="tok",
            )
        )
        try:
            await session.start()
            calls = (
                mock_nio.AsyncClient.return_value.add_response_callback.call_args_list
            )
            assert any(
                call.args[0].__name__ == "_on_room_redact_error_response"
                and call.args[1] is mock_nio.RoomRedactError
                for call in calls
                if len(call.args) >= 2
            )
        finally:
            await session.stop()

    async def test_room_redact_surfaces_first_rate_limit_before_provider_retry(
        self, mock_nio
    ) -> None:
        session = MatrixSession(
            MatrixConfig(
                adapter_id="matrix-1",
                homeserver="https://matrix.example.com",
                user_id="@bot:example.com",
                access_token="tok",
            )
        )
        response = SimpleNamespace(
            status_code="M_LIMIT_EXCEEDED",
            retry_after_ms=4000,
        )
        calls = 0

        async def room_redact(**_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            await session._on_room_redact_error_response(response)
            raise AssertionError("provider retry should have been interrupted")

        session._client = SimpleNamespace(room_redact=room_redact)

        result = await session.room_redact(
            room_id=_ROOM,
            event_id="$owned",
            reason="Deleted by original author via MEDRE relay",
            tx_id="txn-1",
        )
        assert result is response
        assert calls == 1

    async def test_room_redact_interceptor_ignores_non_rate_limit_errors(
        self, mock_nio
    ) -> None:
        session = MatrixSession(
            MatrixConfig(
                adapter_id="matrix-1",
                homeserver="https://matrix.example.com",
                user_id="@bot:example.com",
                access_token="tok",
            )
        )
        response = SimpleNamespace(
            status_code="M_FORBIDDEN",
            retry_after_ms=4000,
            transport_response=SimpleNamespace(status=403),
        )
        # Must not raise: M_FORBIDDEN is a permanent error, not a 429.
        await session._on_room_redact_error_response(response)


@pytest.mark.matrix_sdk
class TestRoomRedactSdkContract:
    """Pin the pinned mindroom-nio redaction contract with real classes."""

    async def test_room_redact_returns_real_redact_response(self) -> None:
        import nio

        session = MatrixSession(
            MatrixConfig(
                adapter_id="matrix-1",
                homeserver="https://matrix.example.com",
                user_id="@bot:example.com",
                access_token="tok",
            )
        )
        expected = nio.RoomRedactResponse.from_dict({"event_id": "$redaction-1"}, _ROOM)
        assert isinstance(expected, nio.RoomRedactResponse)
        assert expected.event_id == "$redaction-1"
        assert expected.room_id == _ROOM

        captured: dict[str, object] = {}

        async def room_redact(**kwargs: object) -> nio.RoomRedactResponse:
            captured.update(kwargs)
            return expected

        session._client = SimpleNamespace(room_redact=room_redact)
        result = await session.room_redact(
            room_id=_ROOM,
            event_id="$owned-copy",
            reason="Deleted by original author via MEDRE relay",
            tx_id="medre_abc",
        )
        assert result is expected
        assert captured["room_id"] == _ROOM
        assert captured["event_id"] == "$owned-copy"
        assert captured["tx_id"] == "medre_abc"

    def test_room_redact_signature_matches_pinned_sdk(self) -> None:
        import inspect

        import nio

        params = list(inspect.signature(nio.AsyncClient.room_redact).parameters)
        assert params == ["self", "room_id", "event_id", "reason", "tx_id"]

    def test_redact_error_has_no_event_id_and_classifies_permanent(self) -> None:
        import nio

        from medre.adapters.matrix.adapter import _is_nio_permanent_response

        error = nio.RoomRedactError.from_dict(
            {"errcode": "M_FORBIDDEN", "error": "denied"}, _ROOM
        )
        assert not hasattr(error, "event_id") or error.event_id is None
        assert _is_nio_permanent_response(error) is True


# ---------------------------------------------------------------------------
# Envelope unit contract
# ---------------------------------------------------------------------------


class TestOutboundEnvelope:
    def test_roundtrip_send_event(self) -> None:
        operation = MatrixOutboundOperation.send_event("m.room.message", {"body": "hi"})
        payload = operation.to_payload()
        assert set(payload) == {MATRIX_OPERATION_KEY}
        decoded = MatrixOutboundOperation.from_payload(payload)
        assert decoded == operation

    def test_roundtrip_redact_event(self) -> None:
        operation = MatrixOutboundOperation.redact("$owned")
        assert operation.event_type is None
        assert operation.content is None
        decoded = MatrixOutboundOperation.from_payload(operation.to_payload())
        assert decoded == operation

    def test_absent_envelope_returns_none(self) -> None:
        assert MatrixOutboundOperation.from_payload({"msgtype": "m.text"}) is None

    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(MatrixOutboundEnvelopeError):
            MatrixOutboundOperation.from_payload(
                {MATRIX_OPERATION_KEY: {"kind": "send_event", "room_id": "!r"}}
            )

    def test_per_kind_field_violations_rejected(self) -> None:
        with pytest.raises(MatrixOutboundEnvelopeError):
            MatrixOutboundOperation.send_event("m.room.message", {})
        with pytest.raises(MatrixOutboundEnvelopeError):
            MatrixOutboundOperation.redact(" ")
        with pytest.raises(MatrixOutboundEnvelopeError):
            MatrixOutboundOperation.from_payload(
                {
                    MATRIX_OPERATION_KEY: {
                        "kind": "redact_event",
                        "redacts_event_id": "$x",
                        "event_type": "m.room.message",
                    }
                }
            )


# ---------------------------------------------------------------------------
# Capability interaction: reply-in-thread vs plain thread
# ---------------------------------------------------------------------------


class TestReplyInThreadCapabilitySemantics:
    _THREAD_ONLY = _rel("thread", native_id="$thread-root")
    _REPLY = _rel("reply", native_id="$latest")

    def test_plain_thread_degrades_on_replies_unsupported_transport(self) -> None:
        caps = AdapterCapabilities(replies="unsupported", threads="fallback")
        event = make_event(
            event_kind="message.created",
            relations=(self._THREAD_ONLY,),
        )
        decision = resolver.decide(event, caps)
        assert decision.delivery_strategy == "fallback_text"
        assert decision.supported is True

    def test_explicit_reply_in_thread_follows_plain_reply_semantics(self) -> None:
        caps = AdapterCapabilities(replies="unsupported", threads="fallback")
        event = make_event(
            event_kind="message.created",
            relations=(self._THREAD_ONLY, self._REPLY),
        )
        decision = resolver.decide(event, caps)
        # Most-severe-wins: the explicit reply relation is unsupported there.
        assert decision.delivery_strategy == "skip"
        assert decision.capability_field == "replies"

    def test_reply_in_thread_is_native_on_matrix(self) -> None:
        caps = AdapterCapabilities(replies="native", threads="native")
        event = make_event(
            event_kind="message.created",
            relations=(self._THREAD_ONLY, self._REPLY),
        )
        decision = resolver.decide(event, caps)
        assert decision.delivery_strategy == "direct"
