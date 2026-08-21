"""Targeted tests for fake-adapter fidelity knobs (D9).

Each test pins one knob on a fake (LXMF signature_validated, MeshCore
``type: PRIV`` direct-event helper, Meshtastic connection_generation
passthrough) to a behavior the real SDK can produce.  Defaults preserve
the historical happy-path behavior.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext


def _make_context(adapter_id: str) -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=lambda _e: asyncio.sleep(0),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _seeded_lxmf() -> FakeLxmfAdapter:
    return FakeLxmfAdapter(
        LxmfConfig(adapter_id="lxmf-fidelity", connection_type="fake").validate()
    )


def _seeded_meshcore() -> FakeMeshCoreAdapter:
    return FakeMeshCoreAdapter(
        MeshCoreConfig(
            adapter_id="meshcore-fidelity", connection_type="fake"
        ).validate()
    )


def _seeded_meshtastic() -> FakeMeshtasticAdapter:
    return FakeMeshtasticAdapter(
        MeshtasticConfig(
            adapter_id="meshtastic-fidelity", connection_type="fake"
        ).validate()
    )


class TestFakeLxmfSignatureValidated:
    """``FakeLxmfAdapter.make_text_event`` accepts ``signature_validated``
    so tests can exercise the unverified-signature attribution branch
    the real SDK can produce (LXMessage.unpack_from_bytes may report
    ``signature_validated=False`` for messages without a verified
    signature).
    """

    async def test_default_signature_is_validated(self) -> None:
        adapter = _seeded_lxmf()

        event = adapter.make_text_event(body="hello", source_name="alice")

        assert event.payload["body"] == "hello"

    async def test_unvalidated_signature_flows_through_attribution(self) -> None:
        """An unvalidated signature does not change attribution: the
        canonical event is structurally identical whether the signature
        is verified or not, because attribution derives from
        announce-derived display names rather than signature state.
        """
        adapter = _seeded_lxmf()

        validated = adapter.make_text_event(
            body="hi", source_name="alice", signature_validated=True
        )
        unvalidated = adapter.make_text_event(
            body="hi", source_name="alice", signature_validated=False
        )

        assert validated.payload["body"] == unvalidated.payload["body"]
        assert validated.event_kind == unvalidated.event_kind
        assert validated.source_adapter == unvalidated.source_adapter
        assert (
            validated.metadata.native.data["lxmf"]["source_hash"]
            == unvalidated.metadata.native.data["lxmf"]["source_hash"]
        )


class TestFakeMeshCoreDirectEvent:
    """``FakeMeshCoreAdapter.make_direct_event`` emits the
    ``type:"PRIV"`` payload shape that real ``CONTACT_MSG_RECV`` events
    carry, so tests can exercise the direct-message classification path.
    """

    def test_make_direct_event_marks_direct_message(self) -> None:
        adapter = _seeded_meshcore()

        event = adapter.make_direct_event(body="hi", sender="abc123")

        meshcore_meta = event.metadata.native.data["meshcore"]
        assert meshcore_meta["is_direct_message"] is True
        assert meshcore_meta["channel"] is None

    def test_priv_event_has_no_channel_index(self) -> None:
        """``type:"PRIV"`` events have no channel index (real
        ``CONTACT_MSG_RECV`` semantics: DMs are not scoped to a channel).
        The codec records this as ``channel=None`` in the canonical
        native metadata.
        """
        adapter = _seeded_meshcore()

        event = adapter.make_direct_event(body="hi")

        meshcore_meta = event.metadata.native.data["meshcore"]
        assert meshcore_meta["is_direct_message"] is True
        assert meshcore_meta["channel"] is None


class TestFakeMeshtasticConnectionGeneration:
    """``FakeMeshtasticAdapter.simulate_inbound`` accepts an optional
    ``connection_generation`` token, mirroring the real adapter's
    staleness check at publish time.
    """

    async def test_default_does_not_set_connection_generation(self) -> None:
        adapter = _seeded_meshtastic()
        await adapter.start(_make_context("meshtastic-fidelity"))

        packet = {
            "fromId": "!sender",
            "toId": "^all",
            "channel": 0,
            "decoded": {
                "portnum": "text_message",
                "text": "hello",
            },
            "id": 12345,
        }
        await adapter.simulate_inbound(packet)

        assert "_connection_generation" not in packet

    async def test_passthrough_records_token_on_packet(self) -> None:
        """When ``connection_generation`` is provided, the fake stores it
        on the packet dict so downstream publish-path tests can verify
        staleness-check coverage (mirrors the real adapter's
        ``_publish_via_session`` re-validation).
        """
        adapter = _seeded_meshtastic()
        await adapter.start(_make_context("meshtastic-fidelity"))

        packet = {
            "fromId": "!sender",
            "toId": "^all",
            "channel": 0,
            "decoded": {
                "portnum": "text_message",
                "text": "hello",
            },
            "id": 12345,
        }
        await adapter.simulate_inbound(packet, connection_generation=7)

        assert packet["_connection_generation"] == 7

    async def test_passthrough_does_not_change_classification(self) -> None:
        """The connection-generation passthrough is metadata-only; the
        classifier's relay/ignore decision for the same packet must be
        identical with and without the token.
        """
        adapter = _seeded_meshtastic()
        await adapter.start(_make_context("meshtastic-fidelity"))

        base_packet = {
            "fromId": "!sender",
            "toId": "^all",
            "channel": 0,
            "decoded": {
                "portnum": "text_message",
                "text": "hello",
            },
            "id": 12345,
        }
        await adapter.simulate_inbound(dict(base_packet))
        await adapter.simulate_inbound(
            dict(base_packet), connection_generation=99
        )

        assert len(adapter.inbound_events) == 2
