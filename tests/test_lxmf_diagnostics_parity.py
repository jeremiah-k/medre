"""LXMF adapter diagnostics parity tests."""

from __future__ import annotations

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.rendering.renderer import RenderingResult


def _config(adapter_id: str = "lxmf-diag-parity") -> LxmfConfig:
    return LxmfConfig(adapter_id=adapter_id, connection_type="fake")


def _packet(
    *,
    content: str = "hello",
    message_id: str = "cd" * 32,
    fields: dict | None = None,
) -> dict:
    return {
        "source_hash": "ab" * 16,
        "destination_hash": "00" * 16,
        "message_id": message_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": fields or {},
        "signature_validated": True,
        "has_fields": bool(fields),
    }


async def test_lxmf_adapter_exposes_session_diagnostics_fields(
    make_adapter_context,
) -> None:
    adapter = LxmfAdapter(_config())
    await adapter.start(make_adapter_context("lxmf-diag-parity"))

    result = RenderingResult(
        event_id="evt-lxmf-diag",
        target_adapter=adapter.adapter_id,
        target_channel="ab" * 16,
        payload={"content": "outbound", "destination_hash": "ab" * 16},
    )
    await adapter.deliver(result)

    diag = adapter.diagnostics()
    session = diag["session"]

    assert "last_message_time" in session
    assert "known_path_count" in session
    assert "propagation_enabled" in session
    assert session["pending_delivery_count"] == 1

    await adapter.stop()


async def test_lxmf_adapter_ingress_counters_are_auditable(
    make_adapter_context,
    inbound_collector,
) -> None:
    adapter = LxmfAdapter(_config("lxmf-ingress-parity"))
    await adapter.start(make_adapter_context("lxmf-ingress-parity"))

    await adapter.simulate_inbound(_packet(content="first", message_id="aa" * 32))
    await adapter.simulate_inbound(_packet(content="first", message_id="aa" * 32))
    await adapter.simulate_inbound(_packet(content="", fields={1: "metadata"}))

    diag = adapter.diagnostics()

    assert len(inbound_collector.events) == 1
    assert diag["classifier_messages_seen"] == 3
    assert diag["classifier_messages_relayed"] == 1
    assert diag["classifier_messages_ignored"] == 2
    assert diag["classifier_messages_non_text_ignored"] == 1
    assert diag["inbound_duplicates_suppressed"] == 1
    assert diag["inbound_published"] == 1
    assert diag["session"]["last_message_time"] is None

    await adapter.stop()


async def test_lxmf_ingress_counters_reset_on_start(
    make_adapter_context,
) -> None:
    adapter = LxmfAdapter(_config("lxmf-ingress-reset"))
    await adapter.start(make_adapter_context("lxmf-ingress-reset"))
    await adapter.simulate_inbound(_packet(content="first"))
    assert adapter.diagnostics()["inbound_published"] == 1

    await adapter.stop()
    await adapter.start(make_adapter_context("lxmf-ingress-reset"))

    diag = adapter.diagnostics()
    assert diag["classifier_messages_seen"] == 0
    assert diag["inbound_published"] == 0
    assert diag["inbound_duplicates_suppressed"] == 0

    await adapter.stop()
