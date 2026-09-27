"""Unit tests for the attachment seam's edge contracts.

Covers the small surfaces the docker tier exercises but the default suite
can reach with plain objects: transfer-permit boundaries, the declared
media-candidate mapping, storage admission guards for attachment bytes,
and association-scoped retained-content reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from medre.adapters.matrix.event_shape import (
    attachment_candidate_from_matrix_media,
)
from medre.core.ingress.content import (
    AttachmentContentUnavailableError,
    AttachmentLimits,
    AttachmentPolicyState,
    AttachmentTransferPermits,
    AttachmentTransferPermitTimeoutError,
)
from medre.core.ingress.types import InboundAttachmentContent
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# AttachmentLimits / AttachmentTransferPermits
# ---------------------------------------------------------------------------


def test_limits_reject_non_positive_and_boolean_values() -> None:
    with pytest.raises(ValueError, match="max_attachment_bytes"):
        AttachmentLimits(max_attachment_bytes=0)
    with pytest.raises(ValueError, match="max_retained_bytes"):
        AttachmentLimits(max_retained_bytes=-1)
    with pytest.raises(ValueError, match="max_attachment_bytes"):
        AttachmentLimits(max_attachment_bytes=True)


def test_permits_reject_invalid_construction() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        AttachmentTransferPermits(max_concurrent=0, acquire_timeout_seconds=1.0)
    with pytest.raises(ValueError, match="acquire_timeout_seconds"):
        AttachmentTransferPermits(max_concurrent=1, acquire_timeout_seconds=0.0)


async def test_permits_close_rejects_new_acquisitions() -> None:
    permits = AttachmentTransferPermits(max_concurrent=1, acquire_timeout_seconds=0.05)
    permits.close()
    with pytest.raises(AttachmentTransferPermitTimeoutError, match="closed"):
        async with permits.acquire():
            pass  # pragma: no cover - never reached


async def test_permits_closed_race_releases_the_acquired_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiter that acquires just as close() lands must not start a transfer."""
    permits = AttachmentTransferPermits(max_concurrent=1, acquire_timeout_seconds=0.05)

    original_acquire = permits._semaphore.acquire

    async def acquire_then_close():
        result = await original_acquire()
        permits.close()
        return result

    monkeypatch.setattr(permits._semaphore, "acquire", acquire_then_close)

    with pytest.raises(AttachmentTransferPermitTimeoutError, match="closed"):
        async with permits.acquire():
            pass  # pragma: no cover - never reached

    assert permits._semaphore._value == 1


# ---------------------------------------------------------------------------
# attachment_candidate_from_matrix_media
# ---------------------------------------------------------------------------


def test_media_candidate_requires_mapping_and_known_kind() -> None:
    assert attachment_candidate_from_matrix_media(None) is None
    assert attachment_candidate_from_matrix_media("not a mapping") is None
    assert attachment_candidate_from_matrix_media({"kind": "sticker"}) is None
    candidate = attachment_candidate_from_matrix_media(
        {
            "kind": "image",
            "filename": "photo.png",
            "mime_type": "image/png",
            "size_bytes": 11,
            "width": 4,
            "height": 4,
        }
    )
    assert candidate is not None
    assert candidate.kind == "image"
    assert candidate.size_bytes == 11
    # Declared form never carries retention state.
    payload = candidate.to_declared_payload()
    assert "content_ref" not in payload
    assert "unavailable_reason" not in payload


# ---------------------------------------------------------------------------
# Storage admission guards and retained-content reads
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _admitted_storage(
    data: bytes = b"payload",
) -> AsyncIterator[tuple[SQLiteStorage, str, str]]:
    from medre.core.events.attachments import AttachmentDescriptor
    from medre.core.events.canonical import NativeMessageRef

    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    event = make_storage_event(
        "evt-media",
        event_kind="message.file",
        payload={
            "body": "file.bin",
            "attachment": AttachmentDescriptor(
                kind="file", filename="file.bin", mime_type="application/octet-stream"
            ).to_declared_payload(),
        },
    )
    ref = NativeMessageRef(
        id="nmr-evt-media",
        event_id="evt-media",
        adapter="fake_transport",
        native_channel_id="ch-0",
        native_message_id="native-evt-media",
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        created_at=event.timestamp,
    )
    result = await storage.admit_ingress(
        event,
        ref,
        "live",
        attachment=InboundAttachmentContent(data=data, declared_size=None),
    )
    fact = result.attachment
    assert fact is not None and fact.retained
    try:
        yield storage, "evt-media", fact.content_ref
    finally:
        await storage.close()


async def test_admission_requires_declared_descriptor() -> None:
    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    event = make_storage_event("evt-plain", event_kind="message.created")
    try:
        with pytest.raises(ValueError, match="without a declared attachment"):
            await storage.admit_ingress(
                event,
                None,
                "live",
                attachment=InboundAttachmentContent(data=b"x", declared_size=None),
            )
    finally:
        await storage.close()


async def test_admission_rejects_non_bytes_payload() -> None:
    from medre.core.events.attachments import AttachmentDescriptor

    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    event = make_storage_event(
        "evt-bad",
        event_kind="message.file",
        payload={
            "attachment": AttachmentDescriptor(kind="file").to_declared_payload(),
        },
    )
    try:
        with pytest.raises(ValueError, match="bytes-like"):
            await storage.admit_ingress(
                event,
                None,
                "live",
                attachment=InboundAttachmentContent(
                    data="not-bytes", declared_size=None
                ),
            )
    finally:
        await storage.close()


async def test_retained_reads_are_association_scoped_and_verified() -> None:
    async with _admitted_storage(b"payload-bytes") as (storage, event_id, content_ref):
        stored = await storage.load_attachment_content(event_id, content_ref)
        assert stored.data == b"payload-bytes"

        with pytest.raises(AttachmentContentUnavailableError, match="association"):
            await storage.load_attachment_content(event_id, "sha256:" + "f" * 64)
        with pytest.raises(AttachmentContentUnavailableError, match="association"):
            await storage.load_attachment_content("evt-other", content_ref)


async def test_missing_blob_fails_with_content_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async with _admitted_storage(b"payload-bytes") as (storage, event_id, content_ref):
        # Simulate a lost blob row (association intact) and verify the honest
        # failure reason; the association FK must be relaxed to delete it.
        storage._db.execute("PRAGMA foreign_keys = OFF")
        storage._db.execute("DELETE FROM attachment_blobs")
        storage._db.commit()
        storage._db.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(
            AttachmentContentUnavailableError, match="missing"
        ) as excinfo:
            await storage.load_attachment_content(event_id, content_ref)
        assert excinfo.value.reason == "content_missing"


async def test_policy_disabled_blocks_retained_reads() -> None:
    from medre.core.storage.sqlite.storage import StorageAttachmentAccess

    async with _admitted_storage(b"payload-bytes") as (storage, event_id, content_ref):
        access = StorageAttachmentAccess(
            storage,
            AttachmentPolicyState(
                enabled=False,
                max_attachment_bytes=10,
                transfer_timeout_seconds=1.0,
            ),
        )
        with pytest.raises(
            AttachmentContentUnavailableError, match="policy is disabled"
        ):
            await access.load_for_event(event_id, content_ref)


async def test_retained_bytes_total_counts_unique_blobs() -> None:
    from medre.core.events.attachments import AttachmentDescriptor
    from medre.core.events.canonical import NativeMessageRef

    async with _admitted_storage(b"payload-bytes") as (
        storage,
        _event_id,
        content_ref,
    ):
        # A second event admitting identical bytes must deduplicate: the blob
        # store holds one row and the total counts it once.
        event = make_storage_event(
            "evt-media-2",
            event_kind="message.file",
            payload={
                "body": "file.bin",
                "attachment": AttachmentDescriptor(
                    kind="file",
                    filename="file.bin",
                    mime_type="application/octet-stream",
                ).to_declared_payload(),
            },
        )
        ref = NativeMessageRef(
            id="nmr-evt-media-2",
            event_id="evt-media-2",
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="native-evt-media-2",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=event.timestamp,
        )
        result = await storage.admit_ingress(
            event,
            ref,
            "live",
            attachment=InboundAttachmentContent(
                data=b"payload-bytes", declared_size=None
            ),
        )
        assert result.attachment is not None and result.attachment.retained
        assert await storage.attachment_retained_bytes() == len(b"payload-bytes")


async def test_admission_rejects_unsupported_provenance() -> None:
    from medre.core.events.attachments import AttachmentDescriptor

    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    event = make_storage_event(
        "evt-bogus",
        event_kind="message.file",
        payload={
            "attachment": AttachmentDescriptor(kind="file").to_declared_payload(),
        },
    )
    try:
        with pytest.raises(ValueError, match="unsupported ingress provenance"):
            await storage.admit_ingress(
                event,
                None,
                "not-a-provenance",
                attachment=InboundAttachmentContent(data=b"x", declared_size=None),
            )
    finally:
        await storage.close()


async def test_duplicate_admission_reports_original_retained_fact() -> None:
    """Re-admitting the same native identity never replaces retained bytes."""
    from medre.core.events.attachments import AttachmentDescriptor
    from medre.core.events.canonical import NativeMessageRef

    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    try:

        def build(event_id: str):
            event = make_storage_event(
                event_id,
                event_kind="message.file",
                payload={
                    "body": "file.bin",
                    "attachment": AttachmentDescriptor(
                        kind="file",
                        filename="file.bin",
                        mime_type="application/octet-stream",
                    ).to_declared_payload(),
                },
            )
            ref = NativeMessageRef(
                id=f"nmr-{event_id}",
                event_id=event_id,
                adapter="fake_transport",
                native_channel_id="ch-0",
                native_message_id="native-same",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=event.timestamp,
            )
            return event, ref

        first = await storage.admit_ingress(
            *build("evt-one"),
            "live",
            attachment=InboundAttachmentContent(data=b"original", declared_size=None),
        )
        assert first.attachment is not None and first.attachment.retained

        second_event, second_ref = build("evt-two")
        second = await storage.admit_ingress(
            second_event,
            second_ref,
            "live",
            attachment=InboundAttachmentContent(
                data=b"replacement", declared_size=None
            ),
        )
        assert second.created is False
        assert second.attachment is not None and second.attachment.retained
        stored = await storage.load_attachment_content(
            "evt-one", second.attachment.content_ref
        )
        assert stored.data == b"original"
    finally:
        await storage.close()
