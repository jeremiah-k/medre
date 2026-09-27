"""Focused behavioral tests: transport-neutral attachment admission.

Covers the durable attachment core described in
``docs/changes/unreleased/217-durable-attachment-relay.md``:

* descriptor invariants (exclusive states, strict parsing, wire retention
  state rejection, content-ref format);
* atomic storage admission: bytes + event + association in one commit,
  measured length over declared, dedup/quota semantics;
* association-scoped loads with explicit failures (forged reference,
  missing/corrupt content) and restart across a closed/reopened storage;
* the synthetic non-Matrix producer/consumer path (no Matrix identifiers
  anywhere).

Run narrowly::

    pytest tests/test_attachment_descriptor.py tests/test_attachment_storage_admission.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from medre.core.events.attachments import (
    ATTACHMENT_KINDS,
    ATTACHMENT_PAYLOAD_KEY,
    ATTACHMENT_UNAVAILABLE_REASONS,
    AttachmentDescriptor,
    AttachmentDescriptorError,
    attachment_descriptor_from_event_payload,
    declared_descriptor_from_event_payload,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.ingress import (
    AttachmentLimits,
    AttachmentPolicyState,
    AttachmentTransferPermits,
    AttachmentTransferPermitTimeoutError,
    InboundAttachmentContent,
)
from medre.core.ingress.content import AttachmentContentUnavailableError
from medre.core.storage.sqlite.storage import (
    SQLiteStorage,
    StorageAttachmentAccess,
)

# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


def _declared() -> AttachmentDescriptor:
    return AttachmentDescriptor(
        kind="image",
        filename="photo.png",
        mime_type="image/png",
        size_bytes=11,
        width=4,
        height=4,
    )


class TestAttachmentDescriptor:
    def test_declared_payload_has_no_retention_state(self) -> None:
        payload = _declared().to_declared_payload()
        assert "content_ref" not in payload
        assert "unavailable_reason" not in payload
        parsed = declared_descriptor_from_event_payload(
            {ATTACHMENT_PAYLOAD_KEY: payload}
        )
        assert parsed is not None
        assert parsed.to_declared_payload() == payload
        # A declared descriptor is not a valid persisted form: the strict
        # persisted parser rejects it until admission rewrites the state.
        with pytest.raises(AttachmentDescriptorError):
            attachment_descriptor_from_event_payload({ATTACHMENT_PAYLOAD_KEY: payload})

    def test_declared_parse_rejects_wire_supplied_retention_state(self) -> None:
        payload = _declared().to_declared_payload()
        forged_ref = {**payload, "content_ref": "sha256:" + "a" * 64}
        with pytest.raises(AttachmentDescriptorError):
            declared_descriptor_from_event_payload({ATTACHMENT_PAYLOAD_KEY: forged_ref})
        forged_reason = {**payload, "unavailable_reason": "oversized"}
        with pytest.raises(AttachmentDescriptorError):
            declared_descriptor_from_event_payload(
                {ATTACHMENT_PAYLOAD_KEY: forged_reason}
            )

    def test_declared_descriptor_equality_and_hash_skip_persisted_validation(
        self,
    ) -> None:
        first = _declared()
        second = _declared()

        assert first == second
        assert hash(first) == hash(second)
        assert first != AttachmentDescriptor(
            kind="image",
            filename="other.png",
            mime_type="image/png",
            size_bytes=11,
            width=4,
            height=4,
        )

    def test_retained_and_unavailable_are_exclusive(self) -> None:
        ref = "sha256:" + "0" * 64
        with pytest.raises(AttachmentDescriptorError):
            AttachmentDescriptor(
                kind="file", content_ref=ref, unavailable_reason="oversized"
            ).validate()
        with pytest.raises(AttachmentDescriptorError):
            AttachmentDescriptor(kind="file").validate()

    def test_content_ref_format_is_enforced(self) -> None:
        for bad in ("not-a-ref", "sha256:ABC", "sha256:" + "g" * 64, ""):
            with pytest.raises(AttachmentDescriptorError):
                AttachmentDescriptor(kind="file", content_ref=bad).validate()

    def test_retained_roundtrip_and_unavailable_reasons(self) -> None:
        ref = "sha256:" + hashlib.sha256(b"x").hexdigest()
        retained = _declared().with_retained_content(ref, 11)
        assert retained.retained and retained.size_bytes == 11
        parsed = attachment_descriptor_from_event_payload(
            {ATTACHMENT_PAYLOAD_KEY: retained.to_payload()}
        )
        assert parsed == retained
        for reason in ATTACHMENT_UNAVAILABLE_REASONS:
            unavailable = _declared().with_unavailable(reason)
            assert not unavailable.retained
            assert (
                attachment_descriptor_from_event_payload(
                    {ATTACHMENT_PAYLOAD_KEY: unavailable.to_payload()}
                )
                == unavailable
            )
        with pytest.raises(AttachmentDescriptorError):
            _declared().with_unavailable("not_a_reason")

    def test_strict_parse_rejects_unknown_fields_and_bool_as_int(self) -> None:
        good = _declared().with_retained_content("sha256:" + "0" * 64, 11)
        with pytest.raises(AttachmentDescriptorError):
            attachment_descriptor_from_event_payload(
                {ATTACHMENT_PAYLOAD_KEY: {**good.to_payload(), "mxc": "mxc://h/m"}}
            )
        with pytest.raises(AttachmentDescriptorError):
            attachment_descriptor_from_event_payload(
                {ATTACHMENT_PAYLOAD_KEY: {**good.to_payload(), "size_bytes": True}}
            )

    @pytest.mark.parametrize("field", ["filename", "mime_type"])
    @pytest.mark.parametrize("bad", ["", 7, True])
    def test_direct_construction_rejects_invalid_optional_strings(
        self, field: str, bad: object
    ) -> None:
        descriptor = AttachmentDescriptor(kind="file", **{field: bad})
        with pytest.raises(AttachmentDescriptorError):
            descriptor.to_declared_payload()

    def test_kinds_are_the_neutral_four(self) -> None:
        assert ATTACHMENT_KINDS == {"image", "audio", "video", "file"}
        with pytest.raises(AttachmentDescriptorError):
            AttachmentDescriptor(kind="sticker").validate()


# ---------------------------------------------------------------------------
# Storage admission (synthetic non-Matrix producer)
# ---------------------------------------------------------------------------

_SYNTHETIC_ADAPTER = "zerotier-relay"  # deliberately non-Matrix everywhere


def _file_event(
    event_id: str,
    declared: AttachmentDescriptor,
    *,
    native_message_id: str | None = None,
) -> CanonicalEvent:
    from medre.core.events.canonical import NativeRef

    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_FILE,
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SYNTHETIC_ADAPTER,
        source_transport_id="synthetic-node-7",
        source_channel_id="vlan42",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={
            "body": declared.filename or "attachment",
            "attachment": declared.to_declared_payload(),
        },
        metadata=EventMetadata(),
        source_native_ref=(
            NativeRef(
                adapter=_SYNTHETIC_ADAPTER,
                native_channel_id="vlan42",
                native_message_id=f"native-{event_id}",
            )
            if native_message_id != ""
            else None
        ),
    )


async def _admit(
    storage: SQLiteStorage,
    event: CanonicalEvent,
    data: bytes,
    *,
    limits: AttachmentLimits | None = None,
) -> object:
    from medre.core.events.canonical import NativeMessageRef

    ref = (
        NativeMessageRef(
            id=f"nmr-{event.event_id}",
            event_id=event.event_id,
            adapter=_SYNTHETIC_ADAPTER,
            native_channel_id="vlan42",
            native_message_id=f"native-{event.event_id}",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(UTC),
        )
        if event.source_native_ref is not None
        else None
    )
    return await storage.admit_ingress(
        event,
        ref,
        "live",
        attachment=InboundAttachmentContent(data=data, declared_size=None),
        attachment_limits=limits,
    )


class TestAtomicAdmission:
    async def test_retained_admission_associates_verified_bytes(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            data = b"synthetic attachment payload"
            event = _file_event("evt-1", _declared())
            result = await _admit(storage, event, data)
            assert result.created is True
            fact = result.attachment
            assert fact is not None and fact.retained
            assert fact.size_bytes == len(data)
            assert fact.content_ref == "sha256:" + hashlib.sha256(data).hexdigest()

            stored = await storage.get("evt-1")
            descriptor = attachment_descriptor_from_event_payload(stored.payload)
            assert descriptor is not None and descriptor.retained
            assert descriptor.content_ref == fact.content_ref
            # Ordinary event serialization never leaks blob bytes.
            assert "data" not in stored.payload

            loaded = await storage.load_attachment_content("evt-1", fact.content_ref)
            assert loaded.data == data
            assert loaded.size_bytes == len(data)
            assert await storage.attachment_retained_bytes() == len(data)
        finally:
            await storage.close()

    async def test_measured_length_beats_dishonest_declared_size(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            data = b"0123456789"
            liar = AttachmentDescriptor(kind="file", filename="small.txt", size_bytes=1)
            event = _file_event("evt-dishonest", liar)
            result = await _admit(storage, event, data)
            fact = result.attachment
            assert fact is not None and fact.retained
            assert fact.size_bytes == 10  # measured, never declared
            stored = await storage.get("evt-dishonest")
            descriptor = attachment_descriptor_from_event_payload(stored.payload)
            assert descriptor is not None and descriptor.size_bytes == 10
        finally:
            await storage.close()

    async def test_duplicate_native_admission_never_replaces_bytes(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            original = b"first bytes"
            event = _file_event("evt-dup", _declared())
            first = await _admit(storage, event, original)
            assert first.created is True
            second = await _admit(storage, event, b"replacement bytes")
            assert second.created is False
            assert second.attachment == first.attachment
            loaded = await storage.load_attachment_content(
                "evt-dup", first.attachment.content_ref
            )
            assert loaded.data == original
            assert await storage.attachment_retained_bytes() == len(original)
        finally:
            await storage.close()

    async def test_identical_content_dedupes_and_consumes_quota_once(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            data = b"shared content across two events"
            limits = AttachmentLimits(
                max_attachment_bytes=64, max_retained_bytes=len(data) + 8
            )
            first = await _admit(
                storage, _file_event("evt-a", _declared()), data, limits=limits
            )
            second = await _admit(
                storage, _file_event("evt-b", _declared()), data, limits=limits
            )
            assert first.attachment.retained and second.attachment.retained
            assert first.attachment.content_ref == second.attachment.content_ref
            # Unique bytes counted once: both events admitted despite a
            # retained budget that fits the content exactly one time.
            assert await storage.attachment_retained_bytes() == len(data)
        finally:
            await storage.close()

    async def test_quota_rejection_admits_event_with_honest_descriptor(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            limits = AttachmentLimits(max_attachment_bytes=1024, max_retained_bytes=10)
            first = await _admit(
                storage,
                _file_event("evt-q1", _declared()),
                b"0123456789",
                limits=limits,
            )
            assert first.attachment.retained
            second = await _admit(
                storage,
                _file_event("evt-q2", _declared()),
                b"abcdefghij",
                limits=limits,
            )
            assert second.created is True  # the event still admits
            fact = second.attachment
            assert fact is not None and not fact.retained
            assert fact.reason == "quota_exceeded"
            stored = await storage.get("evt-q2")
            descriptor = attachment_descriptor_from_event_payload(stored.payload)
            assert descriptor is not None
            assert descriptor.unavailable_reason == "quota_exceeded"
            rejected_ref = "sha256:" + hashlib.sha256(b"abcdefghij").hexdigest()
            # Quota rejection is authoritative: the bytes and event association
            # must not be persisted behind an unavailable descriptor.
            assert await storage.attachment_retained_bytes() == 10
            with pytest.raises(AttachmentContentUnavailableError) as excinfo:
                await storage.load_attachment_content("evt-q2", rejected_ref)
            assert excinfo.value.reason == "association_missing"
        finally:
            await storage.close()

    async def test_empty_attachment_is_retained_with_zero_measured_size(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            result = await _admit(
                storage,
                _file_event("evt-empty", _declared()),
                b"",
                limits=AttachmentLimits(max_attachment_bytes=64, max_retained_bytes=64),
            )
            fact = result.attachment
            assert fact is not None and fact.retained
            assert fact.size_bytes == 0
            assert fact.content_ref == ("sha256:" + hashlib.sha256(b"").hexdigest())
            loaded = await storage.load_attachment_content(
                "evt-empty", fact.content_ref
            )
            assert loaded.data == b""
            assert loaded.size_bytes == 0
            assert await storage.attachment_retained_bytes() == 0
        finally:
            await storage.close()

    async def test_oversized_rejection_uses_measured_length(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            limits = AttachmentLimits(max_attachment_bytes=4, max_retained_bytes=64)
            result = await _admit(
                storage,
                _file_event("evt-big", _declared()),
                b"way more than four bytes",
                limits=limits,
            )
            fact = result.attachment
            assert fact is not None and not fact.retained
            assert fact.reason == "oversized"
            assert await storage.attachment_retained_bytes() == 0
        finally:
            await storage.close()

    async def test_bytes_without_declared_descriptor_raise(self) -> None:
        from medre.core.events.canonical import NativeMessageRef

        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            event = CanonicalEvent(
                event_id="evt-naked",
                event_kind=EventKind.MESSAGE_FILE,
                schema_version=1,
                timestamp=datetime.now(UTC),
                source_adapter=_SYNTHETIC_ADAPTER,
                source_transport_id="synthetic-node-7",
                source_channel_id="vlan42",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": "no descriptor"},
                metadata=EventMetadata(),
            )
            with pytest.raises(ValueError, match="declared attachment descriptor"):
                await storage.admit_ingress(
                    event,
                    NativeMessageRef(
                        id="nmr-naked",
                        event_id="evt-naked",
                        adapter=_SYNTHETIC_ADAPTER,
                        native_channel_id="vlan42",
                        native_message_id="native-evt-naked",
                        native_thread_id=None,
                        native_relation_id=None,
                        direction="inbound",
                        created_at=datetime.now(UTC),
                    ),
                    "live",
                    attachment=InboundAttachmentContent(data=b"bytes"),
                )
        finally:
            await storage.close()


class TestAssociationScopedLoads:
    async def test_forged_reference_from_other_event_fails(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            data = b"private bytes for evt-own"
            result = await _admit(storage, _file_event("evt-own", _declared()), data)
            assert result.attachment.retained
            # A second event cannot name the first event's content_ref.
            await _admit(storage, _file_event("evt-other", _declared()), b"other bytes")
            with pytest.raises(AttachmentContentUnavailableError) as excinfo:
                await storage.load_attachment_content(
                    "evt-other", result.attachment.content_ref
                )
            assert excinfo.value.reason == "association_missing"
        finally:
            await storage.close()

    async def test_missing_and_corrupt_content_fail_explicitly(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            data = b"will be corrupted"
            result = await _admit(
                storage, _file_event("evt-corrupt", _declared()), data
            )
            ref = result.attachment.content_ref
            # Corrupt the stored blob behind the storage API.
            db = storage._require_db()
            with storage._lock:
                db.execute(
                    "UPDATE attachment_blobs SET data = ? WHERE content_ref = ?",
                    (b"tampered-but-same-length!", ref),
                )
                db.commit()
            with pytest.raises(AttachmentContentUnavailableError) as excinfo:
                await storage.load_attachment_content("evt-corrupt", ref)
            assert excinfo.value.reason == "integrity_failed"
        finally:
            await storage.close()

    async def test_restart_reload_from_retained_bytes(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "restart.sqlite")
            data = b"survives the restart"
            ref = None
            storage = SQLiteStorage(db_path)
            await storage.initialize()
            try:
                result = await _admit(
                    storage, _file_event("evt-restart", _declared()), data
                )
                ref = result.attachment.content_ref
            finally:
                await storage.close()

            reopened = SQLiteStorage(db_path)
            await reopened.initialize()
            try:
                loaded = await reopened.load_attachment_content("evt-restart", ref)
                assert loaded.data == data
            finally:
                await reopened.close()

    async def test_policy_disabled_blocks_loads_of_stored_files(self) -> None:
        storage = SQLiteStorage(":memory:")
        await storage.initialize()
        try:
            data = b"previously retained"
            result = await _admit(storage, _file_event("evt-policy", _declared()), data)
            access = StorageAttachmentAccess(
                storage,
                AttachmentPolicyState(
                    enabled=False,
                    max_attachment_bytes=64,
                    transfer_timeout_seconds=1.0,
                ),
            )
            with pytest.raises(AttachmentContentUnavailableError) as excinfo:
                await access.load_for_event("evt-policy", result.attachment.content_ref)
            assert excinfo.value.reason == "policy_disabled"
        finally:
            await storage.close()


class TestTransferPermits:
    async def test_bounded_concurrency_and_close(self) -> None:
        permits = AttachmentTransferPermits(
            max_concurrent=1, acquire_timeout_seconds=0.05
        )
        async with permits.acquire():
            with pytest.raises(AttachmentTransferPermitTimeoutError):
                async with permits.acquire():
                    pass
        permits.close()
        with pytest.raises(AttachmentTransferPermitTimeoutError):
            async with permits.acquire():
                pass

    async def test_waiter_cannot_start_after_close(self) -> None:
        permits = AttachmentTransferPermits(
            max_concurrent=1, acquire_timeout_seconds=1.0
        )
        holder_ready = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_entered = False

        async def hold() -> None:
            async with permits.acquire():
                holder_ready.set()
                await release_holder.wait()

        async def wait_for_slot() -> None:
            nonlocal waiter_entered
            async with permits.acquire():
                waiter_entered = True

        holder = asyncio.create_task(hold())
        await holder_ready.wait()
        waiter = asyncio.create_task(wait_for_slot())
        await asyncio.sleep(0)
        permits.close()
        release_holder.set()
        await holder
        with pytest.raises(AttachmentTransferPermitTimeoutError, match="closed"):
            await waiter
        assert waiter_entered is False

    async def test_release_on_error(self) -> None:
        permits = AttachmentTransferPermits(
            max_concurrent=1, acquire_timeout_seconds=0.05
        )
        with pytest.raises(RuntimeError):
            async with permits.acquire():
                raise RuntimeError("transfer failed mid-flight")
        async with permits.acquire():
            pass


class TestDescriptorContractEdges:
    def test_with_unavailable_rejects_unknown_reason(self) -> None:
        descriptor = _declared()
        with pytest.raises(AttachmentDescriptorError, match="unknown attachment"):
            descriptor.with_unavailable("totally_not_a_reason")

    def test_declared_payload_rejects_persisted_retention_fields(self) -> None:
        from medre.core.events.attachments import (
            declared_descriptor_from_event_payload,
        )

        retained = (
            _declared().with_retained_content("sha256:" + "a" * 64, 11).to_payload()
        )
        with pytest.raises(AttachmentDescriptorError, match="allowed"):
            declared_descriptor_from_event_payload({"attachment": retained})
        unavailable = _declared().with_unavailable("oversized").to_payload()
        with pytest.raises(AttachmentDescriptorError, match="allowed"):
            declared_descriptor_from_event_payload({"attachment": unavailable})

    def test_declared_descriptors_compare_and_hash_without_validation(self) -> None:
        """Declared-form descriptors are equal/hashable pre-retention.

        Their to_payload() would fail full validation (no content_ref and no
        reason yet), so equality must not round-trip through the persisted
        form.
        """
        left = _declared()
        right = _declared()
        assert left == right
        assert hash(left) == hash(right)
        assert left != AttachmentDescriptor(
            kind="image",
            filename="photo.png",
            mime_type="image/png",
            size_bytes=12,
            width=4,
            height=4,
        )

    def test_from_payload_rejects_boolean_numbers(self) -> None:
        with pytest.raises(AttachmentDescriptorError, match="size_bytes"):
            AttachmentDescriptor.from_payload({"kind": "file", "size_bytes": True})
