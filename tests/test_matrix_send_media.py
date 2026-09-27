"""Behavioral tests for the Matrix ``send_media`` egress path.

Covers the renderer's closed ``send_media`` operation (template shape,
roundtrip, fail-closed unavailable descriptors, media-edit rejection) and
the real adapter's delivery: seam-scoped content loading, plaintext and
encrypted upload wire shapes, rate-limit/permanent upload classification,
plus the ``FakeMatrixAdapter`` mirroring of the same semantics.  The
session is a stub standing in for the SDK boundary; the content store is a
stub standing in for the runtime attachment seam.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.outbound import (
    MATRIX_OPERATION_KEY,
    MatrixOutboundOperation,
)
from medre.adapters.matrix.renderer import (
    MatrixAttachmentUnavailableError,
    MatrixNativeMutationError,
    MatrixRenderer,
)
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.events.attachments import (
    ATTACHMENT_PAYLOAD_KEY,
    AttachmentDescriptor,
)
from medre.core.events.canonical import EventRelation, RelationTargetFact
from medre.core.events.kinds import EventKind
from medre.core.ingress.content import (
    AttachmentContentUnavailableError,
    AttachmentPolicyState,
    AttachmentRuntimeSeam,
    AttachmentTransferPermits,
    StoredAttachmentContent,
)
from medre.core.rendering.renderer import RenderingContext, RenderingResult

ROOM_ID = "!r:hs"
SENDER = "@alice:hs"
MXC_URI = "mxc://hs/media1"
CONTENT_REF = "sha256:" + "ab" * 32
STORED_BYTES = b"retained-bytes"
TRANSFER_TIMEOUT_SECONDS = 2.0


def make_config() -> MatrixConfig:
    return MatrixConfig(
        adapter_id="mx",
        homeserver="http://hs",
        user_id="@bot:hs",
        access_token="t",
        room_allowlist={ROOM_ID},
        encryption_mode="plaintext",
    ).validate()


def media_event_dict() -> dict[str, Any]:
    return {
        "room_id": ROOM_ID,
        "sender": SENDER,
        "body": "photo.png",
        "event_id": "$media1",
        "source": {
            "type": "m.room.message",
            "event_id": "$media1",
            "sender": SENDER,
            "origin_server_ts": 1700000000000,
            "content": {
                "msgtype": "m.image",
                "body": "photo.png",
                "filename": "photo.png",
                "url": MXC_URI,
                "info": {"mimetype": "image/png", "size": 11, "w": 2, "h": 2},
            },
        },
        "msgtype": "m.image",
        "server_timestamp": 1700000000000,
        "room_encrypted": False,
        "event_encrypted": False,
        "decrypted": False,
    }


def text_event_dict() -> dict[str, Any]:
    return {
        "room_id": ROOM_ID,
        "sender": SENDER,
        "body": "hello there",
        "event_id": "$text1",
        "source": {
            "type": "m.room.message",
            "event_id": "$text1",
            "sender": SENDER,
            "origin_server_ts": 1700000000000,
            "content": {"msgtype": "m.text", "body": "hello there"},
        },
        "msgtype": "m.text",
        "server_timestamp": 1700000000000,
        "room_encrypted": False,
        "event_encrypted": False,
        "decrypted": False,
    }


def decoded_event(
    event: dict[str, Any],
    *,
    payload_overlay: dict[str, Any] | None = None,
    relations: tuple[EventRelation, ...] = (),
):
    """Decode a normalized dict and overlay persisted payload/relations."""
    from msgspec import structs as msgspec_structs

    canonical = MatrixCodec("mx", make_config()).decode(event, room_id=ROOM_ID)
    payload = dict(canonical.payload)
    if payload_overlay:
        payload.update(payload_overlay)
    return msgspec_structs.replace(canonical, payload=payload, relations=relations)


def retained_media_event(
    descriptor: AttachmentDescriptor | None = None,
    *,
    caption: str = "sunset clip",
) -> Any:
    descriptor = descriptor or AttachmentDescriptor(
        kind="image",
        filename="photo.png",
        mime_type="image/png",
        size_bytes=11,
        width=640,
        height=480,
        content_ref=CONTENT_REF,
    )
    return decoded_event(
        media_event_dict(),
        payload_overlay={
            ATTACHMENT_PAYLOAD_KEY: descriptor.to_payload(),
            "body": caption,
        },
    )


def media_template() -> dict[str, Any]:
    return {
        "msgtype": "m.image",
        "body": "photo",
        "info": {"size": 999, "mimetype": "image/png"},
        "filename": "photo.png",
    }


def send_media_result(
    template: dict[str, Any] | None = None,
    *,
    event_id: str = "evt-1",
    content_ref: str = CONTENT_REF,
) -> RenderingResult:
    operation = MatrixOutboundOperation.send_media(
        content_ref, template if template is not None else media_template()
    )
    return RenderingResult(
        event_id=event_id,
        target_adapter="mx",
        target_channel=ROOM_ID,
        payload=operation.to_payload(),
        metadata={},
    )


def render_ctx() -> RenderingContext:
    return RenderingContext(
        delivery_strategy="direct",
        target_adapter="mx",
        target_channel=ROOM_ID,
        target_platform="matrix",
    )


class StubEgressSession:
    """Boundary stub for the upload + room-send methods the adapter calls."""

    def __init__(
        self,
        *,
        encrypted: bool = False,
        upload_responses: tuple[Any, ...] = (),
    ) -> None:
        self._encrypted = encrypted
        self.crypto_enabled = encrypted
        self.is_live = True
        self.upload_calls: list[dict[str, Any]] = []
        self.room_send_calls: list[dict[str, Any]] = []
        self._upload_responses = list(upload_responses)

    def is_room_encrypted(self, room_id: str) -> bool:
        return self._encrypted

    def encryption_state_known(self, room_id: str) -> bool:
        return True

    def is_room_member(self, room_id: str) -> bool:
        return True

    async def upload_media(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str | None,
        encrypt: bool,
    ) -> Any:
        self.upload_calls.append(
            {
                "data": data,
                "content_type": content_type,
                "filename": filename,
                "encrypt": encrypt,
            }
        )
        index = min(len(self.upload_calls) - 1, len(self._upload_responses) - 1)
        outcome = self._upload_responses[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def room_send(
        self,
        *,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        ignore_unverified_devices: bool,
        tx_id: str,
    ) -> Any:
        self.room_send_calls.append(
            {
                "room_id": room_id,
                "message_type": message_type,
                "content": content,
                "tx_id": tx_id,
            }
        )
        return SimpleNamespace(event_id="$sent-ok")


class StubContentStore:
    """Stands in for the runtime content-access seam."""

    def __init__(
        self,
        stored: StoredAttachmentContent | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._stored = stored
        self._error = error

    async def load_for_event(
        self, event_id: str, content_ref: str
    ) -> StoredAttachmentContent:
        self.calls.append((event_id, content_ref))
        if self._error is not None:
            raise self._error
        assert self._stored is not None
        return self._stored


def make_seam(
    store: StubContentStore | None = None, *, enabled: bool = True
) -> AttachmentRuntimeSeam:
    return AttachmentRuntimeSeam(
        policy=AttachmentPolicyState(
            enabled=enabled,
            max_attachment_bytes=1024,
            transfer_timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        ),
        permits=AttachmentTransferPermits(
            max_concurrent=2, acquire_timeout_seconds=1.0
        ),
        content=store if store is not None else StubContentStore(),
    )


def make_adapter(
    session: StubEgressSession, seam: AttachmentRuntimeSeam | None = None
) -> MatrixAdapter:
    adapter = MatrixAdapter(make_config())
    adapter._session = session
    adapter._started = True

    async def publish_inbound(event: Any) -> None:
        return None

    adapter.ctx = AdapterContext(
        adapter_id="mx",
        publish_inbound=publish_inbound,
        logger=logging.getLogger("test.matrix.send_media"),
        clock=lambda: datetime.now(tz=UTC),
        shutdown_event=asyncio.Event(),
        attachments=seam,
    )
    return adapter


# ---------------------------------------------------------------------------
# Renderer: closed send_media operation
# ---------------------------------------------------------------------------


async def test_render_media_builds_send_media_template_with_roundtrip() -> None:
    event = retained_media_event()
    result = await MatrixRenderer().render(event, render_ctx())

    envelope = result.payload[MATRIX_OPERATION_KEY]
    assert envelope["kind"] == "send_media"
    assert envelope["content_ref"] == CONTENT_REF
    template = envelope["content"]
    assert template["msgtype"] == "m.image"
    assert template["body"] == "sunset clip"  # caption, never the filename
    assert template["filename"] == "photo.png"
    assert template["info"] == {
        "size": 11,
        "mimetype": "image/png",
        "w": 640,
        "h": 480,
    }
    # Provenance envelope rides inside the template; no source MXC is
    # forwarded and no upload reference is pre-inserted.
    assert template["medre"]["envelope"]["canonical_event_id"] == event.event_id
    assert "url" not in template
    assert "file" not in template
    assert result.metadata["matrix_operation"] == "send_media"

    # Roundtrip: the adapter recovers the exact closed operation.
    operation = MatrixOutboundOperation.from_payload(result.payload)
    assert operation.kind == "send_media"
    assert operation.content_ref == CONTENT_REF


async def test_render_media_caption_falls_back_to_filename() -> None:
    event = retained_media_event(caption="")
    result = await MatrixRenderer().render(event, render_ctx())
    template = result.payload[MATRIX_OPERATION_KEY]["content"]
    assert template["body"] == "photo.png"


async def test_render_media_unavailable_descriptor_fails_closed() -> None:
    renderer = MatrixRenderer()
    unavailable = AttachmentDescriptor(
        kind="image",
        size_bytes=11,
        unavailable_reason="policy_disabled",
    )
    event = retained_media_event(unavailable)
    with pytest.raises(
        MatrixAttachmentUnavailableError,
        match="attachment_unavailable:policy_disabled",
    ):
        await renderer.render(event, render_ctx())

    # No descriptor payload at all (event never retained) fails closed too.
    from msgspec import structs as msgspec_structs

    canonical = MatrixCodec("mx", make_config()).decode(
        media_event_dict(), room_id=ROOM_ID
    )
    bare = msgspec_structs.replace(canonical, payload=dict(canonical.payload))
    assert ATTACHMENT_PAYLOAD_KEY not in bare.payload
    with pytest.raises(
        MatrixAttachmentUnavailableError,
        match="attachment_unavailable:not_retained",
    ):
        await renderer.render(bare, render_ctx())


async def test_render_media_generic_file_kind_stays_m_file() -> None:
    descriptor = AttachmentDescriptor(
        kind="file",
        filename="data.bin",
        mime_type="application/octet-stream",
        size_bytes=32,
        content_ref=CONTENT_REF,
    )
    event = retained_media_event(descriptor, caption="")
    result = await MatrixRenderer().render(event, render_ctx())
    template = result.payload[MATRIX_OPERATION_KEY]["content"]
    assert template["msgtype"] == "m.file"


def _edit_relation(target_fact: RelationTargetFact) -> EventRelation:
    return EventRelation(
        "edit", "$target-canonical", None, None, None, target_fact=target_fact
    )


async def test_edit_with_media_msgtype_content_is_rejected() -> None:
    event = retained_media_event()
    relation = _edit_relation(
        RelationTargetFact(
            status="bound_owned",
            adapter="mx",
            native_channel_id=ROOM_ID,
            native_message_id="$orig",
            direction="outbound",
            target_event_kind=EventKind.MESSAGE_CREATED,
        )
    )
    from msgspec import structs as msgspec_structs

    enriched = msgspec_structs.replace(event, relations=(relation,))
    with pytest.raises(MatrixNativeMutationError, match="attachment_edit_unsupported"):
        await MatrixRenderer().render(enriched, render_ctx())


async def test_edit_targeting_stored_media_original_is_rejected() -> None:
    event = decoded_event(
        text_event_dict(),
        relations=(
            _edit_relation(
                RelationTargetFact(
                    status="bound_owned",
                    adapter="mx",
                    native_channel_id=ROOM_ID,
                    native_message_id="$orig",
                    direction="outbound",
                    target_event_kind=EventKind.MESSAGE_FILE,
                )
            ),
        ),
    )
    with pytest.raises(MatrixNativeMutationError, match="attachment_edit_unsupported"):
        await MatrixRenderer().render(event, render_ctx())


async def test_text_edit_of_text_original_renders_replace() -> None:
    event = decoded_event(
        text_event_dict(),
        relations=(
            _edit_relation(
                RelationTargetFact(
                    status="bound_owned",
                    adapter="mx",
                    native_channel_id=ROOM_ID,
                    native_message_id="$orig",
                    direction="outbound",
                    target_event_kind=EventKind.MESSAGE_CREATED,
                )
            ),
        ),
    )
    result = await MatrixRenderer().render(event, render_ctx())
    content = result.payload[MATRIX_OPERATION_KEY]["content"]
    assert content["m.relates_to"] == {
        "rel_type": "m.replace",
        "event_id": "$orig",
    }


# ---------------------------------------------------------------------------
# Real adapter delivery
# ---------------------------------------------------------------------------


async def test_deliver_policy_disabled_fails_closed_without_loading() -> None:
    session = StubEgressSession()
    # Content exists, but generic attachment policy blocks all transfers.
    store = StubContentStore(
        stored=StoredAttachmentContent(
            content_ref=CONTENT_REF,
            size_bytes=len(STORED_BYTES),
            data=STORED_BYTES,
        )
    )
    adapter = make_adapter(session, make_seam(store, enabled=False))

    with pytest.raises(
        AdapterPermanentError, match="attachment_unavailable:policy_disabled"
    ):
        await adapter.deliver(send_media_result())
    assert store.calls == []
    assert session.upload_calls == []
    assert session.room_send_calls == []


async def test_deliver_loads_content_scoped_to_event_and_ref() -> None:
    session = StubEgressSession(
        upload_responses=[
            (SimpleNamespace(content_uri="mxc://hs/up1"), None),
        ]
    )
    store = StubContentStore(
        stored=StoredAttachmentContent(
            content_ref=CONTENT_REF,
            size_bytes=len(STORED_BYTES),
            data=STORED_BYTES,
        )
    )
    adapter = make_adapter(session, make_seam(store))

    await adapter.deliver(send_media_result(event_id="evt-7"))
    assert store.calls == [("evt-7", CONTENT_REF)]


async def test_deliver_forged_content_ref_is_permanent() -> None:
    session = StubEgressSession()
    store = StubContentStore(
        error=AttachmentContentUnavailableError(
            "no retained attachment association for event matching the "
            "requested content reference",
            reason="association_missing",
        )
    )
    adapter = make_adapter(session, make_seam(store))

    with pytest.raises(
        AdapterPermanentError,
        match="attachment_unavailable:association_missing",
    ):
        await adapter.deliver(send_media_result())
    assert store.calls == [("evt-1", CONTENT_REF)]
    assert session.upload_calls == []


async def test_deliver_plaintext_room_uploads_and_sends_url() -> None:
    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = StubEgressSession(
        encrypted=False,
        upload_responses=[(SimpleNamespace(content_uri="mxc://hs/up1"), None)],
    )
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    result = await adapter.deliver(send_media_result())

    assert session.upload_calls[0]["data"] == STORED_BYTES
    assert session.upload_calls[0]["content_type"] == "image/png"
    assert session.upload_calls[0]["filename"] == "photo.png"
    assert session.upload_calls[0]["encrypt"] is False
    wire = session.room_send_calls[0]["content"]
    assert wire["url"] == "mxc://hs/up1"
    # Measured stored length wins over the template's declared size (999).
    assert wire["info"]["size"] == 7
    assert "file" not in wire
    assert wire["msgtype"] == "m.image"
    assert result.native_message_id == "$sent-ok"
    assert result.native_channel_id == ROOM_ID


async def test_deliver_encrypted_room_sends_file_with_keys() -> None:
    keys = {
        "v": "v2",
        "key": {
            "kty": "oct",
            "alg": "A256CTR",
            "ext": True,
            "k": "k" * 43,
            "key_ops": ["encrypt", "decrypt"],
        },
        "iv": "i" * 24,
        "hashes": {"sha256": "h" * 43},
    }
    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = StubEgressSession(
        encrypted=True,
        upload_responses=[
            (SimpleNamespace(content_uri="mxc://hs/up1"), keys),
        ],
    )
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    result = await adapter.deliver(send_media_result())

    assert session.upload_calls[0]["encrypt"] is True
    wire = session.room_send_calls[0]["content"]
    assert "url" not in wire  # plaintext url key must be absent
    file_obj = wire["file"]
    assert file_obj["url"] == "mxc://hs/up1"
    assert file_obj["v"] == "v2"
    assert file_obj["key"] == keys["key"]
    assert file_obj["iv"] == keys["iv"]
    assert file_obj["hashes"] == keys["hashes"]
    assert file_obj["mimetype"] == "image/png"
    assert result.native_message_id == "$sent-ok"


async def test_deliver_reads_room_encryption_once_for_safety_and_upload() -> None:
    """One encryption snapshot feeds both the safety gate and the upload.

    A sync landing between two separate reads could flip the delivery
    between encrypted and plaintext after the policy gate passed; the gate
    and the upload must consume the same value.
    """

    class FlippingEgressSession(StubEgressSession):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(encrypted=True, **kwargs)
            self.encryption_reads = 0

        def is_room_encrypted(self, room_id: str) -> bool:
            self.encryption_reads += 1
            return self._encrypted if self.encryption_reads == 1 else False

    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = FlippingEgressSession(
        upload_responses=[
            (
                SimpleNamespace(content_uri="mxc://hs/up1"),
                {"v": "v2", "key": {"k": "k" * 43}, "iv": "i" * 24, "hashes": {}},
            )
        ],
    )
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    await adapter.deliver(send_media_result())

    assert session.encryption_reads == 1
    assert session.upload_calls[0]["encrypt"] is True
    wire = session.room_send_calls[0]["content"]
    assert "url" not in wire  # the encrypted snapshot drove an encrypted send
    assert "file" in wire


async def test_upload_rate_limit_is_transient_and_opens_cooldown() -> None:
    response = SimpleNamespace(
        status_code=429, errcode="M_LIMIT_EXCEEDED", retry_after_ms=2000
    )
    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = StubEgressSession(upload_responses=[(response, None)])
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    with pytest.raises(AdapterSendError) as excinfo:
        await adapter.deliver(send_media_result())
    assert excinfo.value.transient is True
    assert "rate-limited" in str(excinfo.value)
    assert adapter._outbound_rate_limit_events == 1
    assert adapter._outbound_cooldown_until > 0

    # The server-directed cooldown fails the immediate second attempt fast,
    # before any further upload.
    with pytest.raises(AdapterSendError, match="cooldown") as second:
        await adapter.deliver(send_media_result())
    assert second.value.transient is True
    assert len(session.upload_calls) == 1


async def test_upload_exhausted_network_error_is_transient() -> None:
    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = StubEgressSession(upload_responses=[OSError("connection reset")])
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    with pytest.raises(AdapterSendError) as excinfo:
        await adapter.deliver(send_media_result())
    assert excinfo.value.transient is True
    assert "media upload transport failure" in str(excinfo.value)
    assert adapter._transient_delivery_failures == 1
    assert adapter._permanent_delivery_failures == 0


async def test_upload_server_5xx_is_transient() -> None:
    response = SimpleNamespace(
        errcode="M_UNKNOWN",
        transport_response=SimpleNamespace(status=503),
    )
    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = StubEgressSession(upload_responses=[(response, None)])
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    with pytest.raises(AdapterSendError) as excinfo:
        await adapter.deliver(send_media_result())
    assert excinfo.value.transient is True
    assert "HTTP 503" in str(excinfo.value)
    assert adapter._transient_delivery_failures == 1
    assert adapter._permanent_delivery_failures == 0


async def test_upload_permanent_error_is_permanent() -> None:
    response = SimpleNamespace(errcode="M_FORBIDDEN")
    stored = StoredAttachmentContent(
        content_ref=CONTENT_REF, size_bytes=7, data=STORED_BYTES
    )
    session = StubEgressSession(upload_responses=[(response, None)])
    adapter = make_adapter(session, make_seam(StubContentStore(stored=stored)))

    with pytest.raises(AdapterPermanentError) as excinfo:
        await adapter.deliver(send_media_result())
    assert "media upload failed" in str(excinfo.value)
    assert "M_FORBIDDEN" in str(excinfo.value)
    assert excinfo.value.transient is False


# ---------------------------------------------------------------------------
# FakeMatrixAdapter seam semantics
# ---------------------------------------------------------------------------


def make_fake_context(seam: AttachmentRuntimeSeam | None) -> AdapterContext:
    async def publish_inbound(event: Any) -> None:
        return None

    return AdapterContext(
        adapter_id="fake_mx",
        publish_inbound=publish_inbound,
        logger=logging.getLogger("test.matrix.fake"),
        clock=lambda: datetime.now(tz=UTC),
        shutdown_event=asyncio.Event(),
        attachments=seam,
    )


async def test_fake_adapter_records_media_bytes_through_seam() -> None:
    store = StubContentStore(
        stored=StoredAttachmentContent(
            content_ref=CONTENT_REF,
            size_bytes=len(STORED_BYTES),
            data=STORED_BYTES,
        )
    )
    fake = FakeMatrixAdapter("fake_mx")
    await fake.start(make_fake_context(make_seam(store)))

    handoff = await fake.deliver(send_media_result(event_id="evt-9"))

    assert store.calls == [("evt-9", CONTENT_REF)]
    assert fake.sent_media == [(CONTENT_REF, STORED_BYTES)]
    operation = fake.sent_operations[0]
    assert operation.kind == "send_media"
    assert operation.content_ref == CONTENT_REF
    assert handoff.native_message_id == "$fake_evt-9"


async def test_fake_adapter_forged_content_ref_is_permanent() -> None:
    store = StubContentStore(
        error=AttachmentContentUnavailableError(
            "forged reference", reason="association_missing"
        )
    )
    fake = FakeMatrixAdapter("fake_mx")
    await fake.start(make_fake_context(make_seam(store)))

    with pytest.raises(
        AdapterPermanentError,
        match="attachment_unavailable:association_missing",
    ):
        await fake.deliver(send_media_result(event_id="evt-10"))
    assert fake.sent_media == []
    assert fake.sent_operations == []
    assert fake.delivered_payloads == []


async def test_fake_adapter_disabled_policy_fails_closed() -> None:
    store = StubContentStore(
        stored=StoredAttachmentContent(
            content_ref=CONTENT_REF,
            size_bytes=len(STORED_BYTES),
            data=STORED_BYTES,
        )
    )
    fake = FakeMatrixAdapter("fake_mx")
    await fake.start(make_fake_context(make_seam(store, enabled=False)))

    with pytest.raises(
        AdapterPermanentError, match="attachment_unavailable:policy_disabled"
    ):
        await fake.deliver(send_media_result(event_id="evt-disabled"))

    assert store.calls == []
    assert fake.sent_media == []
    assert fake.sent_operations == []
    assert fake.delivered_payloads == []


async def test_fake_adapter_without_seam_accepts_send_media() -> None:
    fake = FakeMatrixAdapter("fake_mx")
    await fake.start(make_fake_context(seam=None))

    handoff = await fake.deliver(send_media_result(event_id="evt-11"))

    assert fake.sent_media == []
    operation = fake.sent_operations[0]
    assert operation.kind == "send_media"
    assert operation.content_ref == CONTENT_REF
    assert handoff.native_message_id == "$fake_evt-11"
