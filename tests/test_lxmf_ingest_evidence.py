"""LXMF SDK-boundary delivery evidence.

The pinned LXMRouter proves each inbound packet to the sender BEFORE
invoking the registered delivery callback, and every later drop is
silent — a sender-side DELIVERED state therefore says nothing about
whether the message reached the app.  The session counts every delivery
callback invocation so diagnostics separate "the router never handed
the message over" from later-stage losses.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig
from tests.helpers.async_utils import wait_until


def _make_config(**overrides) -> LxmfConfig:
    defaults = dict(adapter_id="lxmf-ingest-1", connection_type="fake")
    defaults.update(overrides)
    return LxmfConfig(**defaults)


def _stub_message(content: bytes = b"ingest-nonce-1") -> SimpleNamespace:
    """Minimal LXMessage stand-in for the normalisation boundary."""
    return SimpleNamespace(
        source_hash=b"\x01" * 16,
        destination_hash=b"\x02" * 16,
        hash=b"\x03" * 32,
        timestamp=datetime.now(timezone.utc).timestamp(),
        title=b"",
        content=content,
        fields={},
        signature_validated=True,
        method=None,
        state=None,
    )


async def test_delivery_callback_invocations_are_counted() -> None:
    """Each SDK delivery-callback invocation increments the counter."""
    received: list[dict] = []
    session = LxmfSession(_make_config(), "lxmf-ingest-1")
    await session.start(lambda payload: received.append(payload))
    try:
        session._on_lxmf_delivery(_stub_message())
        await asyncio.sleep(0)
        assert session.diagnostics().deliveries_received == 1
        assert len(received) == 1
        assert received[0]["content"] == "ingest-nonce-1"

        session._on_lxmf_delivery(_stub_message(b"ingest-nonce-2"))
        await asyncio.sleep(0)
        assert session.diagnostics().deliveries_received == 2
        assert len(received) == 2
    finally:
        await session.stop()


async def test_delivery_counter_stops_after_stop() -> None:
    """Callbacks arriving after stop() are dropped and not counted."""
    received: list[dict] = []
    session = LxmfSession(_make_config(), "lxmf-ingest-1")
    await session.start(lambda payload: received.append(payload))
    await session.stop()

    session._on_lxmf_delivery(_stub_message())
    await asyncio.sleep(0)
    assert session.diagnostics().deliveries_received == 0
    assert received == []


async def test_adapter_diagnostics_expose_delivery_count(
    make_adapter_context,
) -> None:
    """The adapter's diagnostics surface the session delivery count."""
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-ingest-1")
    await adapter.start(ctx)
    try:
        adapter.session._on_lxmf_delivery(_stub_message())
        await wait_until(
            lambda: adapter.diagnostics()["inbound_published"] == 1,
            timeout=5.0,
        )

        diag = adapter.diagnostics()
        assert diag["session"]["deliveries_received"] == 1
        assert diag["inbound_published"] == 1
        assert diag["classifier_messages_seen"] == 1
        assert diag["classifier_messages_relayed"] == 1
    finally:
        await adapter.stop()


async def test_adapter_diagnostics_default_to_zero(
    make_adapter_context,
) -> None:
    """A session with no inbound traffic reports zero deliveries."""
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-ingest-1")
    await adapter.start(ctx)
    try:
        diag = adapter.diagnostics()
        assert diag["session"]["deliveries_received"] == 0
        assert diag["inbound_published"] == 0
    finally:
        await adapter.stop()
