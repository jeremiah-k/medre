"""Direct unit tests for RelationEnricher.

Tests target the ``RelationEnricher(storage=..., logger=...)`` API
with ``await enrich_for_target(event, target_adapter=..., target_channel=...)``.
Behaviour is derived from the original ``PipelineRunner._enrich_relations_for_target``
so that these tests lock existing semantics before mechanical extraction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.metadata import EventMetadata, NativeMetadata

from medre.core.planning.relation_enricher import RelationEnricher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStorage:
    """Duck-typed fake storage providing ``get`` and ``list_native_refs_for_event``.

    Supports optional per-method failure injection for resilience tests.
    """

    def __init__(
        self,
        events: dict[str, CanonicalEvent] | None = None,
        native_refs: dict[str, list[NativeMessageRef]] | None = None,
    ) -> None:
        self._events: dict[str, CanonicalEvent] = events or {}
        self._native_refs: dict[str, list[NativeMessageRef]] = native_refs or {}
        self._get_raises: bool = False
        self._list_raises: bool = False

    def set_get_raises(self, value: bool = True) -> None:
        self._get_raises = value

    def set_list_raises(self, value: bool = True) -> None:
        self._list_raises = value

    async def get(self, event_id: str) -> CanonicalEvent | None:
        if self._get_raises:
            raise RuntimeError("simulated storage.get failure")
        return self._events.get(event_id)

    async def list_native_refs_for_event(
        self, event_id: str
    ) -> list[NativeMessageRef]:
        if self._list_raises:
            raise RuntimeError("simulated list_native_refs failure")
        return list(self._native_refs.get(event_id, []))


class _MissingMethodsStorage:
    """Storage that lacks optional methods entirely."""

    pass


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_event(
    event_id: str = "src-001",
    relations: tuple[EventRelation, ...] = (),
    payload: dict[str, Any] | None = None,
    metadata: EventMetadata | None = None,
    source_adapter: str = "src",
    source_transport_id: str = "node-1",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"text": "hello"},
        metadata=metadata or EventMetadata(),
    )


def _make_target_event(
    event_id: str = "target-001",
    text: str = "target body",
    source_transport_id: str = "node-t",
    native_data: dict[str, Any] | None = None,
) -> CanonicalEvent:
    meta = EventMetadata()
    if native_data:
        meta = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src",
        source_transport_id=source_transport_id,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": text, "text": text},
        metadata=meta,
    )


def _make_nref(
    adapter: str = "mesh-1",
    channel: str | None = "0",
    msg_id: str = "native-msg-001",
    thread_id: str | None = None,
) -> NativeRef:
    return NativeRef(
        adapter=adapter,
        native_channel_id=channel,
        native_message_id=msg_id,
        native_thread_id=thread_id,
    )


def _make_native_message_ref(
    event_id: str = "target-001",
    adapter: str = "mesh-1",
    channel: str | None = "0",
    msg_id: str = "native-msg-001",
    direction: str = "outbound",
) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-{msg_id}",
        event_id=event_id,
        adapter=adapter,
        native_channel_id=channel,
        native_message_id=msg_id,
        native_thread_id=None,
        native_relation_id=None,
        direction=direction,  # type: ignore[arg-type]
        metadata={},
        created_at=_ts(),
    )


def _rel(
    target_event_id: str | None = "target-001",
    relation_type: str = "reply",
    target_native_ref: NativeRef | None = None,
    fallback_text: str | None = None,
    key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EventRelation:
    return EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata or {},
    )


def _make_enricher(
    storage: Any = None,
    logger: logging.Logger | None = None,
) -> RelationEnricher:
    return RelationEnricher(
        storage=storage or FakeStorage(),
        logger=logger or logging.getLogger("test.relation_enricher"),
    )


# ===================================================================
# No-relations fast path
# ===================================================================


class TestNoRelations:
    """Event with no relations returns the same object unchanged."""

    async def test_no_relations_returns_same_event(self) -> None:
        storage = FakeStorage()
        enricher = _make_enricher(storage)
        event = _make_event(event_id="empty-rel", relations=())
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result is event

    async def test_no_relations_no_channel_returns_same_event(self) -> None:
        storage = FakeStorage()
        enricher = _make_enricher(storage)
        event = _make_event(event_id="empty-rel-nc", relations=())
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel=None
        )
        assert result is event


# ===================================================================
# Exact channel native ref match
# ===================================================================


class TestExactChannelMatch:
    """When target_channel is specified, only exact channel match is accepted."""

    async def test_exact_channel_match_enriches(self) -> None:
        nref = _make_native_message_ref(
            event_id="target-001", adapter="mesh-1", channel="0", msg_id="right-msg"
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-ch-exact",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        enriched_rel = result.relations[0]
        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref.adapter == "mesh-1"
        assert enriched_rel.target_native_ref.native_channel_id == "0"
        assert enriched_rel.target_native_ref.native_message_id == "right-msg"

    async def test_wrong_channel_not_enriched_when_target_channel_set(self) -> None:
        nref = _make_native_message_ref(
            event_id="target-001", adapter="mesh-1", channel="1", msg_id="wrong-ch"
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-ch-wrong",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].target_native_ref is None


# ===================================================================
# Adapter-only fallback (target_channel=None)
# ===================================================================


class TestAdapterOnlyFallback:
    """When target_channel is None, adapter-only match is used."""

    async def test_adapter_match_without_channel(self) -> None:
        nref = _make_native_message_ref(
            event_id="target-001",
            adapter="mesh-1",
            channel="5",
            msg_id="adapter-only-msg",
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-adap-only",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel=None
        )
        enriched_rel = result.relations[0]
        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref.adapter == "mesh-1"
        assert enriched_rel.target_native_ref.native_message_id == "adapter-only-msg"

    async def test_no_adapter_match_returns_same(self) -> None:
        nref = _make_native_message_ref(
            event_id="target-001",
            adapter="other-adapter",
            channel="0",
            msg_id="other-msg",
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-adap",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel=None
        )
        # No matching adapter — relation unchanged, event identity preserved.
        assert result is event


# ===================================================================
# Incompatible native ref stripped / ignored
# ===================================================================


class TestIncompatibleRefStripped:
    """Incompatible ref (same adapter, wrong channel) is stripped when target_channel set."""

    async def test_wrong_channel_ref_stripped_when_target_channel_set(self) -> None:
        existing_ref = _make_nref(adapter="mesh-1", channel="9", msg_id="stale")
        storage = FakeStorage(native_refs={})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-strip",
            relations=(
                _rel(
                    target_event_id="target-001",
                    target_native_ref=existing_ref,
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # Same adapter but wrong channel → stripped to None (no match found).
        assert result.relations[0].target_native_ref is None
        assert result is not event


# ===================================================================
# Different-adapter ref preserved
# ===================================================================


class TestDifferentAdapterRefPreserved:
    """Native ref from a different adapter is not replaced."""

    async def test_different_adapter_ref_kept(self) -> None:
        other_ref = _make_nref(
            adapter="other-adapter", channel="ch", msg_id="other-msg"
        )
        storage = FakeStorage(native_refs={})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-diff-adap",
            relations=(
                _rel(
                    target_event_id="target-001",
                    target_native_ref=other_ref,
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].target_native_ref is other_ref

    async def test_different_adapter_with_channel_fallback(self) -> None:
        """When a different-adapter ref exists and no target-adapter match,
        the different-adapter ref is kept."""
        other_ref = _make_nref(
            adapter="other-adapter", channel="ch", msg_id="other-msg"
        )
        # Only a ref for a different adapter in storage.
        nref = _make_native_message_ref(
            event_id="target-001",
            adapter="other-adapter",
            channel="0",
            msg_id="storage-other",
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-diff-adap-2",
            relations=(
                _rel(
                    target_event_id="target-001",
                    target_native_ref=other_ref,
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # The existing ref from other-adapter is kept because it's not
        # for the target adapter — no stripping occurs.
        assert result.relations[0].target_native_ref is other_ref


# ===================================================================
# Fallback text from target event body/text
# ===================================================================


class TestFallbackTextFromTarget:
    """fallback_text populated from target event payload body/text."""

    async def test_fallback_text_set_from_body(self) -> None:
        target = _make_target_event(event_id="target-001", text="original message")
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-fb-body",
            relations=(_rel(target_event_id="target-001", fallback_text=None),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].fallback_text == "original message"

    async def test_fallback_text_set_from_text_key(self) -> None:
        target = CanonicalEvent(
            event_id="target-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=_ts(),
            source_adapter="src",
            source_transport_id="node-t",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "from text key"},
            metadata=EventMetadata(),
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-fb-text",
            relations=(_rel(target_event_id="target-001", fallback_text=None),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].fallback_text == "from text key"


# ===================================================================
# original_text metadata
# ===================================================================


class TestOriginalTextMetadata:
    """metadata['original_text'] populated from target event body/text."""

    async def test_original_text_set_when_missing(self) -> None:
        target = _make_target_event(event_id="target-001", text="original text")
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-orig-text",
            relations=(_rel(target_event_id="target-001", metadata={}),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_text") == "original text"


# ===================================================================
# original_sender_displayname from displayname → longname
# ===================================================================


class TestOriginalSenderDisplayname:
    """original_sender_displayname from target native metadata."""

    async def test_displayname_used(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            native_data={"displayname": "Alice"},
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-dn",
            relations=(_rel(target_event_id="target-001", metadata={}),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_sender_displayname") == "Alice"

    async def test_longname_fallback(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            native_data={"longname": "Bob"},
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-dn-long",
            relations=(_rel(target_event_id="target-001", metadata={}),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_sender_displayname") == "Bob"

    async def test_displayname_preferred_over_longname(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            native_data={"displayname": "Alice", "longname": "!alice1234"},
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-dn-pref",
            relations=(_rel(target_event_id="target-001", metadata={}),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_sender_displayname") == "Alice"


# ===================================================================
# original_sender from sender → source_transport_id
# ===================================================================


class TestOriginalSender:
    """original_sender from target native metadata or source_transport_id."""

    async def test_sender_from_native_data(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            source_transport_id="node-t",
            native_data={"sender": "alice@matrix"},
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-snd",
            relations=(_rel(target_event_id="target-001", metadata={}),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_sender") == "alice@matrix"

    async def test_sender_fallback_to_source_transport_id(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            source_transport_id="node-transport-42",
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-snd-fb",
            relations=(_rel(target_event_id="target-001", metadata={}),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert (
            result.relations[0].metadata.get("original_sender") == "node-transport-42"
        )


# ===================================================================
# Existing metadata not overwritten
# ===================================================================


class TestExistingMetadataNotOverwritten:
    """Pre-existing metadata fields are never replaced."""

    async def test_original_text_not_overwritten(self) -> None:
        target = _make_target_event(event_id="target-001", text="new text")
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-ow",
            relations=(
                _rel(
                    target_event_id="target-001",
                    metadata={"original_text": "old text"},
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_text") == "old text"

    async def test_original_sender_displayname_not_overwritten(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            native_data={"displayname": "New Name"},
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-ow-dn",
            relations=(
                _rel(
                    target_event_id="target-001",
                    metadata={"original_sender_displayname": "Old Name"},
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert (
            result.relations[0].metadata.get("original_sender_displayname")
            == "Old Name"
        )

    async def test_original_sender_not_overwritten(self) -> None:
        target = _make_target_event(
            event_id="target-001",
            native_data={"sender": "new_sender"},
        )
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-ow-snd",
            relations=(
                _rel(
                    target_event_id="target-001",
                    metadata={"original_sender": "old_sender"},
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].metadata.get("original_sender") == "old_sender"

    async def test_existing_correct_channel_ref_not_replaced(self) -> None:
        """Existing ref matching target adapter and channel is kept as-is."""
        existing = _make_nref(adapter="mesh-1", channel="0", msg_id="existing")
        storage = FakeStorage()
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-replace",
            relations=(
                _rel(
                    target_event_id="target-001",
                    target_native_ref=existing,
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].target_native_ref is existing
        assert result is event


# ===================================================================
# Missing storage target — no crash
# ===================================================================


class TestMissingStorageTarget:
    """storage.get returning None does not crash or enrich text."""

    async def test_missing_target_no_crash(self) -> None:
        storage = FakeStorage(events={})  # No target event stored.
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-missing",
            relations=(_rel(target_event_id="nonexistent"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # No crash; relation unchanged (no native ref, no fallback text).
        assert result.relations[0].target_event_id == "nonexistent"
        assert result.relations[0].fallback_text is None


# ===================================================================
# storage.get failure — no crash / logged
# ===================================================================


class TestStorageGetFailure:
    """storage.get raising is caught and logged without crash."""

    async def test_get_failure_no_crash(self) -> None:
        storage = FakeStorage()
        storage.set_get_raises(True)
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-get-fail",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # No crash; event identity may change or stay the same.
        assert result.relations[0].target_event_id == "target-001"

    async def test_get_failure_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        storage = FakeStorage()
        storage.set_get_raises(True)
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-get-fail-log",
            relations=(_rel(target_event_id="target-001"),),
        )
        with caplog.at_level(logging.DEBUG, logger="test.relation_enricher"):
            await enricher.enrich_for_target(
                event, target_adapter="mesh-1", target_channel="0"
            )
        # The enricher should log the failure (DEBUG or WARNING level).
        assert any(
            "Failed" in r.message or "failed" in r.message.lower()
            for r in caplog.records
        )


# ===================================================================
# list_native_refs failure — no crash / logged
# ===================================================================


class TestListNativeRefsFailure:
    """list_native_refs_for_event raising is caught and logged."""

    async def test_list_failure_no_crash(self) -> None:
        storage = FakeStorage()
        storage.set_list_raises(True)
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-list-fail",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].target_event_id == "target-001"

    async def test_list_failure_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        storage = FakeStorage()
        storage.set_list_raises(True)
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-list-fail-log",
            relations=(_rel(target_event_id="target-001"),),
        )
        with caplog.at_level(logging.DEBUG, logger="test.relation_enricher"):
            await enricher.enrich_for_target(
                event, target_adapter="mesh-1", target_channel="0"
            )
        assert any(
            "Failed" in r.message or "failed" in r.message.lower()
            for r in caplog.records
        )


# ===================================================================
# Storage missing optional methods — graceful
# ===================================================================


class TestStorageMissingMethods:
    """Storage objects without list_native_refs_for_event or get are handled."""

    async def test_no_list_method(self) -> None:
        storage = _MissingMethodsStorage()
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-list",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # Graceful: relation unchanged, no crash.
        assert result.relations[0].target_native_ref is None

    async def test_no_get_method(self) -> None:
        storage = _MissingMethodsStorage()
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-no-get",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # No crash, no text enrichment.
        assert result.relations[0].fallback_text is None


# ===================================================================
# Multiple relations — order preserved
# ===================================================================


class TestMultipleRelationsOrderPreserved:
    """Order of multiple relations is preserved after enrichment."""

    async def test_order_preserved(self) -> None:
        nref_a = _make_native_message_ref(
            event_id="target-a", adapter="mesh-1", channel="0", msg_id="msg-a"
        )
        nref_b = _make_native_message_ref(
            event_id="target-b", adapter="mesh-1", channel="0", msg_id="msg-b"
        )
        nref_c = _make_native_message_ref(
            event_id="target-c", adapter="mesh-1", channel="0", msg_id="msg-c"
        )
        storage = FakeStorage(
            native_refs={
                "target-a": [nref_a],
                "target-b": [nref_b],
                "target-c": [nref_c],
            }
        )
        enricher = _make_enricher(storage)
        rels = (
            _rel(target_event_id="target-a", relation_type="reply"),
            _rel(target_event_id="target-b", relation_type="reaction", key="👍"),
            _rel(target_event_id="target-c", relation_type="edit"),
        )
        event = _make_event(event_id="src-order", relations=rels)
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert len(result.relations) == 3
        assert result.relations[0].relation_type == "reply"
        assert result.relations[0].target_native_ref.native_message_id == "msg-a"
        assert result.relations[1].relation_type == "reaction"
        assert result.relations[1].target_native_ref.native_message_id == "msg-b"
        assert result.relations[2].relation_type == "edit"
        assert result.relations[2].target_native_ref.native_message_id == "msg-c"


# ===================================================================
# Relation types preserved (reaction / edit / delete / thread)
# ===================================================================


class TestRelationTypesPreserved:
    """All relation_type values pass through enrichment unchanged."""

    @pytest.mark.parametrize(
        "rel_type",
        ["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_relation_type_preserved(self, rel_type: str) -> None:
        nref = _make_native_message_ref(
            event_id="target-001", adapter="mesh-1", channel="0", msg_id="msg-type"
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id=f"src-type-{rel_type}",
            relations=(_rel(target_event_id="target-001", relation_type=rel_type),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].relation_type == rel_type
        assert result.relations[0].target_native_ref is not None


# ===================================================================
# Fallback text already set — not overwritten
# ===================================================================


class TestFallbackTextAlreadySet:
    """Pre-existing fallback_text is not overwritten by target event text."""

    async def test_fallback_text_preserved(self) -> None:
        target = _make_target_event(event_id="target-001", text="new body text")
        storage = FakeStorage(events={"target-001": target})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-fb-set",
            relations=(
                _rel(target_event_id="target-001", fallback_text="kept original"),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].fallback_text == "kept original"


# ===================================================================
# Relation without target_event_id — unchanged
# ===================================================================


class TestRelationWithoutTargetEventId:
    """Relation with target_event_id=None is left unchanged."""

    async def test_no_target_event_id_skipped(self) -> None:
        storage = FakeStorage()
        enricher = _make_enricher(storage)
        native = _make_nref(adapter="matrix", channel="!r:s", msg_id="$m1")
        event = _make_event(
            event_id="src-no-tid",
            relations=(
                _rel(
                    target_event_id=None,
                    target_native_ref=native,
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result is event
        assert result.relations[0].target_event_id is None
        assert result.relations[0].target_native_ref is native


# ===================================================================
# Thread ID preservation
# ===================================================================


class TestThreadIdPreservation:
    """native_thread_id from matching ref is preserved."""

    async def test_thread_id_in_enriched_ref(self) -> None:
        nref = NativeMessageRef(
            id="nref-thread-1",
            event_id="target-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="msg-thread",
            native_thread_id="thread-42",
            native_relation_id=None,
            direction="outbound",
            metadata={},
            created_at=_ts(),
        )
        storage = FakeStorage(native_refs={"target-001": [nref]})
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-thread",
            relations=(_rel(target_event_id="target-001"),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        assert result.relations[0].target_native_ref is not None
        assert result.relations[0].target_native_ref.native_thread_id == "thread-42"


# ===================================================================
# Original event immutability
# ===================================================================


class TestOriginalEventImmutability:
    """The original event is never mutated by enrichment."""

    async def test_original_event_unchanged(self) -> None:
        nref = _make_native_message_ref(
            event_id="target-001", adapter="mesh-1", channel="0", msg_id="msg-imm"
        )
        target = _make_target_event(event_id="target-001", text="target text")
        storage = FakeStorage(
            events={"target-001": target},
            native_refs={"target-001": [nref]},
        )
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-imm",
            relations=(_rel(target_event_id="target-001"),),
        )
        # Snapshot original state.
        orig_rel = event.relations[0]
        assert orig_rel.target_native_ref is None
        assert orig_rel.fallback_text is None

        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )

        # Result is a new event.
        assert result is not event
        # Original event's relations untouched.
        assert event.relations[0].target_native_ref is None
        assert event.relations[0].fallback_text is None
        # Result has enrichment.
        assert result.relations[0].target_native_ref is not None
        assert result.relations[0].fallback_text == "target text"


# ===================================================================
# Existing native ref for same adapter, no channel specified
# ===================================================================


class TestExistingAdapterMatchNoChannel:
    """When target_channel=None and existing ref matches adapter, kept as-is."""

    async def test_adapter_match_no_channel_kept(self) -> None:
        existing = _make_nref(adapter="mesh-1", channel="5", msg_id="existing-no-ch")
        storage = FakeStorage()
        enricher = _make_enricher(storage)
        event = _make_event(
            event_id="src-adap-match-no-ch",
            relations=(
                _rel(
                    target_event_id="target-001",
                    target_native_ref=existing,
                ),
            ),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel=None
        )
        assert result is event
        assert result.relations[0].target_native_ref is existing


# ===================================================================
# Mixed: one enriched, one unchanged
# ===================================================================


class TestMixedEnrichment:
    """Only relations that need enrichment are changed; others preserved."""

    async def test_mixed_enrichment(self) -> None:
        nref = _make_native_message_ref(
            event_id="target-001", adapter="mesh-1", channel="0", msg_id="enriched"
        )
        target = _make_target_event(event_id="target-001", text="enriched text")
        storage = FakeStorage(
            events={"target-001": target},
            native_refs={"target-001": [nref]},
        )
        enricher = _make_enricher(storage)
        # First relation: needs enrichment (no native ref).
        # Second relation: already complete (has matching native ref).
        existing = _make_nref(adapter="mesh-1", channel="0", msg_id="already")
        rels = (
            _rel(target_event_id="target-001"),
            _rel(
                target_event_id="target-002",
                target_native_ref=existing,
            ),
        )
        event = _make_event(event_id="src-mixed", relations=rels)
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # First relation enriched.
        assert result.relations[0].target_native_ref is not None
        assert result.relations[0].target_native_ref.native_message_id == "enriched"
        assert result.relations[0].fallback_text == "enriched text"
        # Second relation kept as-is.
        assert result.relations[1].target_native_ref is existing


# ===================================================================
# Non-dict target payload — no crash, no text enrichment
# ===================================================================


class TestNonDictTargetPayload:
    """Target event with non-dict payload is handled gracefully."""

    async def test_string_payload_no_crash(self) -> None:
        """A target event whose payload is a string (not dict) does not crash
        and does not populate fallback_text from the payload."""

        class _StringPayloadEvent:
            """Minimal duck-typed event with a non-dict payload."""

            event_id = "target-str"
            event_kind = "message.created"
            schema_version = 1
            timestamp = _ts()
            source_adapter = "src"
            source_transport_id = "node-t"
            source_channel_id = None
            parent_event_id = None
            lineage: tuple[()] = ()
            relations: tuple[()] = ()
            payload: str = "just a string, not a dict"
            metadata = EventMetadata()

        target = _StringPayloadEvent()

        class _Storage:
            """Minimal storage returning a non-dict-payload target."""

            async def get(self, event_id: str) -> object:
                return target if event_id == "target-str" else None

            async def list_native_refs_for_event(
                self, event_id: str
            ) -> list[NativeMessageRef]:
                return []

        enricher = _make_enricher(storage=_Storage())
        event = _make_event(
            event_id="src-str-payload",
            relations=(_rel(target_event_id="target-str", fallback_text=None),),
        )
        result = await enricher.enrich_for_target(
            event, target_adapter="mesh-1", target_channel="0"
        )
        # No crash; fallback_text remains None (non-dict payload skipped).
        assert result.relations[0].fallback_text is None
