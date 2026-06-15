"""Tests for LXMF adapter display-name enrichment wiring.

Covers :meth:`LxmfAdapter._resolve_display_name` and
:meth:`LxmfAdapter._enrich_with_display_name` plus the full ingress
path through :meth:`LxmfAdapter.simulate_inbound`.

The session's ``resolve_display_name`` is mocked throughout — a parallel
change adds the real implementation to ``LxmfSession``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.config.adapters.lxmf import LxmfConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> LxmfConfig:
    """Build a fake-mode LxmfConfig with optional overrides."""
    defaults: dict = dict(adapter_id="lxmf-1", connection_type="fake")
    defaults.update(overrides)
    return LxmfConfig(**defaults)


def _make_text_packet(
    content: str = "hello",
    source_hash: str = "ab" * 16,
    msg_id: str = "cd" * 32,
    source_name: str | None = None,
) -> dict:
    """Build a minimal text-classifiable LXMF packet dict.

    ``source_name`` is omitted by default so enrichment is exercised.
    """
    packet: dict = {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }
    if source_name is not None:
        packet["source_name"] = source_name
    return packet


def _make_adapter_with_mock_session() -> LxmfAdapter:
    """Build an unstarted LxmfAdapter whose session is a MagicMock.

    The private enrichment methods only touch ``self._session`` and do
    not require the adapter to be started, so this is sufficient for
    unit testing ``_resolve_display_name`` / ``_enrich_with_display_name``.
    """
    adapter = LxmfAdapter(_make_config())
    adapter._session = MagicMock()
    return adapter


# ===================================================================
# _resolve_display_name
# ===================================================================


def test_resolve_display_name_delegates_to_session() -> None:
    """_resolve_display_name forwards the hash to the session and returns its result."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"

    result = adapter._resolve_display_name("deadbeef")

    assert result == "Alice"
    adapter._session.resolve_display_name.assert_called_once_with("deadbeef")


def test_resolve_display_name_returns_none_when_session_none() -> None:
    """When the session is unavailable the resolution yields None."""
    adapter = LxmfAdapter(_make_config())
    adapter._session = None

    assert adapter._resolve_display_name("deadbeef") is None


def test_resolve_display_name_returns_none_for_falsy_source_hash() -> None:
    """None or empty source_hash short-circuits to None without touching the session."""
    adapter = _make_adapter_with_mock_session()

    assert adapter._resolve_display_name(None) is None
    assert adapter._resolve_display_name("") is None
    adapter._session.resolve_display_name.assert_not_called()


def test_resolve_display_name_returns_none_when_session_raises() -> None:
    """A raising session never propagates; None is returned instead."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.side_effect = RuntimeError("boom")

    assert adapter._resolve_display_name("deadbeef") is None


# ===================================================================
# _enrich_with_display_name
# ===================================================================


def test_enrich_injects_resolved_name_when_packet_lacks_source_name() -> None:
    """An empty source_name is filled from the announce cache."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="")

    adapter._enrich_with_display_name(packet)

    assert packet["source_name"] == "Alice"


def test_enrich_injects_when_source_name_key_missing() -> None:
    """A missing source_name key is also filled from the announce cache."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"
    packet = _make_text_packet(source_hash="abcdef0123456789")
    assert "source_name" not in packet

    adapter._enrich_with_display_name(packet)

    assert packet["source_name"] == "Alice"


def test_enrich_preserves_message_carried_source_name() -> None:
    """A message-carried source_name always wins over the announce cache."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="Bob")

    adapter._enrich_with_display_name(packet)

    assert packet["source_name"] == "Bob"
    adapter._session.resolve_display_name.assert_not_called()


def test_enrich_no_injection_when_resolution_returns_none() -> None:
    """When the cache has no entry the source_name stays empty."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = None
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="")

    adapter._enrich_with_display_name(packet)

    assert packet.get("source_name", "") == ""


def test_enrich_no_injection_when_source_hash_missing() -> None:
    """A packet without a source_hash is left untouched and never crashes."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"
    packet = _make_text_packet(source_hash="abcdef0123456789")
    packet.pop("source_hash")

    adapter._enrich_with_display_name(packet)

    assert "source_hash" not in packet
    assert "source_name" not in packet
    adapter._session.resolve_display_name.assert_not_called()


def test_enrich_never_raises_on_exception() -> None:
    """A raising session is swallowed; the packet is left unchanged."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.side_effect = RuntimeError("boom")
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="")

    # Must not raise.
    adapter._enrich_with_display_name(packet)

    assert packet.get("source_name", "") == ""


def test_enrich_does_not_inject_whitespace_source_name() -> None:
    """Whitespace-only source_name is treated as empty and replaced."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="   ")

    adapter._enrich_with_display_name(packet)

    assert packet["source_name"] == "Alice"


def test_enrich_skips_when_source_hash_is_non_str() -> None:
    """A non-string source_hash is ignored without crashing."""
    adapter = _make_adapter_with_mock_session()
    adapter._session.resolve_display_name.return_value = "Alice"
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="")
    packet["source_hash"] = 123

    adapter._enrich_with_display_name(packet)

    assert packet.get("source_name", "") == ""
    adapter._session.resolve_display_name.assert_not_called()


def test_enrich_no_op_when_session_none() -> None:
    """Enrichment is a no-op when the session is unavailable."""
    adapter = LxmfAdapter(_make_config())
    adapter._session = None
    packet = _make_text_packet(source_hash="abcdef0123456789", source_name="")

    adapter._enrich_with_display_name(packet)

    assert packet.get("source_name", "") == ""


def test_enrich_catches_unexpected_exception_outside_resolve() -> None:
    """The outer try/except in _enrich_with_display_name catches
    exceptions that escape _resolve_display_name itself (e.g. a bug
    in the wrapper), ensuring ingestion never fails."""
    adapter = _make_adapter_with_mock_session()
    packet = {"source_hash": "abcdef0123456789"}  # no source_name key
    # Replace _resolve_display_name with one that raises directly,
    # bypassing its internal try/except.
    adapter._resolve_display_name = Mock(side_effect=RuntimeError("wrapper bug"))
    # Should NOT raise — outer catch-all swallows it.
    adapter._enrich_with_display_name(packet)
    # No injection happened because the exception aborted before assignment.
    assert "source_name" not in packet


# ===================================================================
# End-to-end ingress via simulate_inbound
# ===================================================================


async def test_simulate_inbound_carries_display_name_in_native_metadata(
    make_adapter_context, inbound_collector
) -> None:
    """The resolved display name flows through enrich -> codec into native metadata.

    Exercises the full ingress path: enrichment injects source_name, the
    codec projects it into ``lxmf.display_name`` native metadata.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    # Replace the fake session with a mock that resolves display names.
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = "Alice"

    packet = _make_text_packet(source_hash="abcdef0123456789", content="hi")
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    assert event.metadata.native.data["lxmf.display_name"] == "Alice"


async def test_simulate_inbound_without_display_name_has_no_lxmf_display_name_key(
    make_adapter_context, inbound_collector
) -> None:
    """When the cache misses, no lxmf.display_name key is emitted.

    Enrichment injects nothing, so the codec leaves the native metadata
    without an ``lxmf.display_name`` entry.
    """
    adapter = LxmfAdapter(_make_config())
    ctx = make_adapter_context("lxmf-1")
    await adapter.start(ctx)
    adapter._session = MagicMock()
    adapter._session.resolve_display_name.return_value = None

    packet = _make_text_packet(source_hash="abcdef0123456789", content="hi")
    await adapter.simulate_inbound(packet)

    assert len(inbound_collector.events) == 1
    event = inbound_collector.events[0]
    assert "lxmf.display_name" not in event.metadata.native.data
