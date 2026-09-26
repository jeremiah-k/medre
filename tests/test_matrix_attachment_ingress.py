"""Behavioral tests for the durable Matrix attachment ingress path.

Drives ``MatrixAdapter._on_room_message`` (and, for the not-wired-admission
gate, ``_prepare_inbound_attachment`` directly) with normalized event dicts
and a stub session that simulates the SDK boundary: a bounded download and
a decryption step backed by the pinned nio crypto helpers.

The durable admission callable is a recorder; the tests assert the
consumer-visible contract: which descriptor reached admission, whether
plaintext bytes were admitted alongside it, and which failures defer the
durable work versus admit descriptor-only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.errors import (
    MatrixMediaTransientError,
    MatrixMediaUnavailableError,
)
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events.attachments import ATTACHMENT_PAYLOAD_KEY
from medre.core.events.kinds import EventKind
from medre.core.ingress.content import (
    AttachmentPolicyState,
    AttachmentRuntimeSeam,
    AttachmentTransferPermits,
)
from medre.core.ingress.types import (
    AdmissionResult,
    DurableIngressDeferredError,
)

ROOM_ID = "!r:hs"
SENDER = "@alice:hs"
MXC_URI = "mxc://hs/media1"
ENCRYPTED_MXC_URI = "mxc://hs/enc1"
MEDIA_BYTES = b"photo-bytes"  # 11 bytes, matches the declared wire size.
DECLARED_SIZE = 11
TRANSFER_TIMEOUT_SECONDS = 2.0
MAX_ATTACHMENT_BYTES = 1024

_MISSING = object()


def make_config() -> MatrixConfig:
    return MatrixConfig(
        adapter_id="mx",
        homeserver="http://hs",
        user_id="@bot:hs",
        access_token="t",
        room_allowlist={ROOM_ID},
        encryption_mode="plaintext",
    ).validate()


def media_content() -> dict[str, Any]:
    return {
        "msgtype": "m.image",
        "body": "photo.png",
        "filename": "photo.png",
        "url": MXC_URI,
        "info": {
            "mimetype": "image/png",
            "size": DECLARED_SIZE,
            "w": 2,
            "h": 2,
        },
    }


def media_event(
    content: dict[str, Any] | None = None,
    *,
    event_id: str = "$media1",
) -> dict[str, Any]:
    """Build the normalized event dict the session boundary hands over."""
    resolved = content if content is not None else media_content()
    return {
        "room_id": ROOM_ID,
        "sender": SENDER,
        "body": resolved.get("body", ""),
        "event_id": event_id,
        "source": {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": SENDER,
            "origin_server_ts": 1700000000000,
            "content": resolved,
        },
        "msgtype": resolved.get("msgtype", "m.image"),
        "server_timestamp": 1700000000000,
        "room_encrypted": False,
        "event_encrypted": False,
        "decrypted": False,
    }


class StubMatrixSession:
    """Boundary stub exposing only the media methods the adapter calls."""

    def __init__(self) -> None:
        self.download_calls: list[dict[str, Any]] = []
        self.decrypt_calls: list[dict[str, Any]] = []
        self.download_result: Any = b""
        self.decrypt_result: Any = b""
        self.decrypt_fn: Callable[[bytes, Any], bytes] | None = None

    async def download_media(
        self, *, mxc: str, max_bytes: int, timeout_seconds: float
    ) -> bytes:
        self.download_calls.append(
            {
                "mxc": mxc,
                "max_bytes": max_bytes,
                "timeout_seconds": timeout_seconds,
            }
        )
        result = self.download_result
        if isinstance(result, Exception):
            raise result
        return result

    def decrypt_media_attachment(
        self, *, ciphertext: bytes, file_info: object
    ) -> bytes:
        """Sync method: the adapter invokes it via ``asyncio.to_thread``."""
        self.decrypt_calls.append({"ciphertext": ciphertext, "file_info": file_info})
        if self.decrypt_fn is not None:
            return self.decrypt_fn(ciphertext, file_info)
        result = self.decrypt_result
        if isinstance(result, Exception):
            raise result
        return result


class AdmissionRecorder:
    """Stands in for the core durable-admission callable."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def events(self) -> list[Any]:
        return [call["event"] for call in self.calls]

    async def __call__(
        self, event: Any, provenance: str, attachment: Any = None
    ) -> AdmissionResult:
        self.calls.append(
            {"event": event, "provenance": provenance, "attachment": attachment}
        )
        return AdmissionResult(
            event_id=event.event_id,
            created=True,
            provenance=provenance,
            work_status="completed",
        )


class _RefusingContentStore:
    """Ingress never loads retained content; any call is a test failure."""

    async def load_for_event(self, event_id: str, content_ref: str) -> None:
        raise AssertionError(
            "ingress path must never load retained content "
            f"({event_id}, {content_ref})"
        )


def make_seam(
    *,
    enabled: bool = True,
    permits: AttachmentTransferPermits | None = None,
) -> AttachmentRuntimeSeam:
    return AttachmentRuntimeSeam(
        policy=AttachmentPolicyState(
            enabled=enabled,
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
            transfer_timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        ),
        permits=(
            permits
            if permits is not None
            else AttachmentTransferPermits(
                max_concurrent=2, acquire_timeout_seconds=1.0
            )
        ),
        content=_RefusingContentStore(),
    )


def make_adapter(
    session: StubMatrixSession,
    *,
    seam: AttachmentRuntimeSeam | None,
    admit: Any = _MISSING,
) -> tuple[MatrixAdapter, Any]:
    """Build a started adapter wired to the stub boundary.

    ``admit=_MISSING`` installs a fresh recorder; ``admit=None`` leaves
    ``ctx.admit_inbound`` unset to exercise the not-wired gate.
    """
    adapter = MatrixAdapter(make_config())
    adapter._session = session
    adapter._started = True
    if admit is _MISSING:
        admit = AdmissionRecorder()

    async def publish_inbound(event: Any) -> None:
        return None

    adapter.ctx = AdapterContext(
        adapter_id="mx",
        publish_inbound=publish_inbound,
        logger=logging.getLogger("test.matrix.attachment_ingress"),
        clock=lambda: datetime.now(tz=UTC),
        shutdown_event=asyncio.Event(),
        admit_inbound=admit,
        attachments=seam,
    )
    return adapter, admit


def decode_event(adapter: MatrixAdapter, event: dict[str, Any]) -> Any:
    return adapter.get_codec().decode(event, room_id=ROOM_ID)


async def test_codec_media_message_yields_file_kind_and_native_media_projection() -> (
    None
):
    codec = MatrixCodec("mx", make_config())
    canonical = codec.decode(media_event(), room_id=ROOM_ID)
    assert canonical.event_kind == EventKind.MESSAGE_FILE
    assert canonical.metadata.native.data["matrix"]["media"] == {
        "kind": "image",
        "encrypted": False,
        "mxc_uri": MXC_URI,
        "filename": "photo.png",
        "mime_type": "image/png",
        "size_bytes": DECLARED_SIZE,
        "width": 2,
        "height": 2,
    }


def test_codec_encrypted_flag_follows_content_file_presence() -> None:
    pytest.importorskip("nio")
    from nio.crypto.attachments import encrypt_attachment

    _, keys = encrypt_attachment(MEDIA_BYTES)
    content = {
        "msgtype": "m.image",
        "body": "photo.png",
        "filename": "photo.png",
        "file": {**keys, "url": ENCRYPTED_MXC_URI, "mimetype": "image/png"},
        "info": {"mimetype": "image/png", "size": DECLARED_SIZE},
    }
    canonical = MatrixCodec("mx", make_config()).decode(
        media_event(content), room_id=ROOM_ID
    )
    assert canonical.event_kind == EventKind.MESSAGE_FILE
    media = canonical.metadata.native.data["matrix"]["media"]
    assert media["encrypted"] is True
    assert media["mxc_uri"] == ENCRYPTED_MXC_URI


async def test_policy_disabled_admits_descriptor_only_without_fetch() -> None:
    session = StubMatrixSession()
    session.download_result = MEDIA_BYTES
    adapter, admit = make_adapter(session, seam=make_seam(enabled=False))

    await adapter._on_room_message(media_event(), "live")

    assert session.download_calls == []
    assert len(admit.calls) == 1
    assert admit.calls[0]["attachment"] is None
    descriptor = admit.events[0].payload[ATTACHMENT_PAYLOAD_KEY]
    assert descriptor["unavailable_reason"] == "policy_disabled"
    assert "content_ref" not in descriptor
    assert adapter._inbound_attachment_unavailable == 1
    assert adapter._inbound_attachment_retained == 0


async def test_history_provenance_is_suppressed_without_fetch() -> None:
    session = StubMatrixSession()
    session.download_result = MEDIA_BYTES
    adapter, admit = make_adapter(session, seam=make_seam())

    await adapter._on_room_message(media_event(), "history")

    assert session.download_calls == []
    assert len(admit.calls) == 1
    assert admit.calls[0]["provenance"] == "history"
    assert admit.calls[0]["attachment"] is None
    descriptor = admit.events[0].payload[ATTACHMENT_PAYLOAD_KEY]
    assert descriptor["unavailable_reason"] == "history_suppressed"
    assert adapter._inbound_attachment_unavailable == 1


async def test_missing_admit_callable_classifies_not_retained() -> None:
    session = StubMatrixSession()
    session.download_result = MEDIA_BYTES
    adapter, _ = make_adapter(session, seam=make_seam(), admit=None)

    event = media_event()
    canonical = decode_event(adapter, event)
    prepared_event, attachment = await adapter._prepare_inbound_attachment(
        event, canonical, "live"
    )

    assert attachment is None
    descriptor = prepared_event.payload[ATTACHMENT_PAYLOAD_KEY]
    assert descriptor["unavailable_reason"] == "not_retained"
    assert session.download_calls == []


async def test_missing_mxc_uri_admits_malformed_source() -> None:
    session = StubMatrixSession()
    session.download_result = MEDIA_BYTES
    adapter, admit = make_adapter(session, seam=make_seam())

    content = media_content()
    del content["url"]
    await adapter._on_room_message(media_event(content), "live")

    assert session.download_calls == []
    assert len(admit.calls) == 1
    assert admit.calls[0]["attachment"] is None
    descriptor = admit.events[0].payload[ATTACHMENT_PAYLOAD_KEY]
    assert descriptor["unavailable_reason"] == "malformed_source"


async def test_fetch_success_admits_plaintext_bytes_with_declared_descriptor() -> None:
    session = StubMatrixSession()
    session.download_result = MEDIA_BYTES
    adapter, admit = make_adapter(session, seam=make_seam())

    await adapter._on_room_message(media_event(), "live")

    assert session.download_calls == [
        {
            "mxc": MXC_URI,
            "max_bytes": MAX_ATTACHMENT_BYTES,
            "timeout_seconds": TRANSFER_TIMEOUT_SECONDS,
        }
    ]
    assert len(admit.calls) == 1
    content = admit.calls[0]["attachment"]
    assert content.data == MEDIA_BYTES
    assert content.declared_size == DECLARED_SIZE
    descriptor = admit.events[0].payload[ATTACHMENT_PAYLOAD_KEY]
    # Declared in-flight form only: no content_ref, no unavailable reason.
    assert descriptor == {
        "kind": "image",
        "filename": "photo.png",
        "mime_type": "image/png",
        "size_bytes": DECLARED_SIZE,
        "width": 2,
        "height": 2,
    }
    assert adapter._inbound_attachment_retained == 1
    assert adapter._inbound_attachment_unavailable == 0


async def test_encrypted_media_is_decrypted_before_admission() -> None:
    pytest.importorskip("nio")
    from nio.crypto.attachments import decrypt_attachment, encrypt_attachment

    ciphertext, keys = encrypt_attachment(MEDIA_BYTES)
    file_obj = {**keys, "url": ENCRYPTED_MXC_URI, "mimetype": "image/png"}
    content = {
        "msgtype": "m.image",
        "body": "photo.png",
        "filename": "photo.png",
        "file": file_obj,
        "info": {"mimetype": "image/png", "size": DECLARED_SIZE},
    }

    def decrypt_via_pinned_sdk(data: bytes, file_info: object) -> bytes:
        assert isinstance(file_info, dict)
        return decrypt_attachment(
            data,
            file_info["key"]["k"],
            file_info["hashes"]["sha256"],
            file_info["iv"],
        )

    session = StubMatrixSession()
    session.download_result = ciphertext
    session.decrypt_fn = decrypt_via_pinned_sdk
    adapter, admit = make_adapter(session, seam=make_seam())

    await adapter._on_room_message(media_event(content), "live")

    assert session.download_calls[0]["mxc"] == ENCRYPTED_MXC_URI
    assert len(admit.calls) == 1
    assert session.decrypt_calls[0]["file_info"] == file_obj
    assert session.decrypt_calls[0]["ciphertext"] == ciphertext
    assert admit.calls[0]["attachment"].data == MEDIA_BYTES
    media = admit.events[0].metadata.native.data["matrix"]["media"]
    assert media["encrypted"] is True
    assert adapter._inbound_attachment_retained == 1


async def test_permanent_media_unavailable_admits_descriptor_only() -> None:
    session = StubMatrixSession()
    session.download_result = MatrixMediaUnavailableError(
        "media no longer exists", reason="content_missing"
    )
    adapter, admit = make_adapter(session, seam=make_seam())

    await adapter._on_room_message(media_event(), "live")

    assert len(admit.calls) == 1
    assert admit.calls[0]["attachment"] is None
    descriptor = admit.events[0].payload[ATTACHMENT_PAYLOAD_KEY]
    assert descriptor["unavailable_reason"] == "content_missing"
    assert adapter._inbound_attachment_unavailable == 1
    assert adapter._inbound_attachment_deferred == 0


async def test_transient_media_error_defers_ingress_without_admission() -> None:
    session = StubMatrixSession()
    session.download_result = MatrixMediaTransientError("upstream connection reset")
    adapter, admit = make_adapter(session, seam=make_seam())

    with pytest.raises(DurableIngressDeferredError) as excinfo:
        await adapter._on_room_message(media_event(), "live")

    assert isinstance(excinfo.value.event_id, str) and excinfo.value.event_id
    assert any("transient failure" in reason for reason in excinfo.value.reasons)
    assert admit.calls == []
    assert adapter._inbound_attachment_deferred == 1
    assert adapter._inbound_attachment_unavailable == 0


async def test_permit_contention_defers_ingress_without_fetch() -> None:
    permits = AttachmentTransferPermits(max_concurrent=1, acquire_timeout_seconds=0.01)
    session = StubMatrixSession()
    session.download_result = MEDIA_BYTES
    adapter, admit = make_adapter(session, seam=make_seam(permits=permits))

    async with permits.acquire():
        with pytest.raises(DurableIngressDeferredError) as excinfo:
            await adapter._on_room_message(media_event(), "live")
    assert any("permit" in reason for reason in excinfo.value.reasons)
    assert session.download_calls == []
    assert admit.calls == []
    assert adapter._inbound_attachment_deferred == 1
    # The gate still works after the failed acquisition: no permit leaked.
    async with asyncio.timeout(1.0):
        async with permits.acquire():
            pass
