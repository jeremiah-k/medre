"""Adapter stale-guard granularity boundary tests.

These tests exercise :meth:`AdapterContract._is_stale_event` directly on
real adapter classes (LXMF for the microsecond default granularity,
Matrix for the millisecond override) so the granularity knob added to
the adapter contract is verified end-to-end without network calls.

The guard floors ``_start_time`` to the adapter's
``_event_timestamp_granularity_us`` before comparing to ``event.timestamp``:

* LXMF / Meshtastic / MeshCore (microsecond-resolution transports) keep
  the default ``_event_timestamp_granularity_us = 1`` and compare
  timestamps exactly — a live event created within the startup
  microsecond is *not* dropped.
* Matrix (``origin_server_ts`` millisecond granularity) overrides to
  ``_event_timestamp_granularity_us = 1_000`` so an event created within
  the startup millisecond is *not* dropped as backlog.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.contracts.adapter import AdapterContract
from medre.core.events import CanonicalEvent, EventMetadata

_START = datetime(2026, 1, 1, 12, 0, 0, 800, tzinfo=timezone.utc)
"""Shared microsecond-anchored start time for granularity assertions."""


def _make_event(timestamp: datetime, *, source_adapter: str = "test") -> CanonicalEvent:
    """Build a minimal CanonicalEvent with the given timestamp."""
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=timestamp,
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_lxmf_adapter() -> LxmfAdapter:
    """Build an LXMF adapter in fake mode for stale-guard assertions."""
    config = LxmfConfig(adapter_id="lxmf-1", connection_type="fake")
    return LxmfAdapter(config)


def _make_matrix_adapter() -> MatrixAdapter:
    """Build a Matrix adapter (without starting it) for stale-guard tests."""
    from medre.config.adapters.matrix import MatrixConfig

    cfg = MatrixConfig(
        adapter_id="matrix-1",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    return MatrixAdapter(cfg)


def test_lxmf_pre_start_in_same_millisecond_is_stale() -> None:
    """LXMF inherits the microsecond default — events compare exactly.

    600µs before start, same millisecond.
    """
    adapter = _make_lxmf_adapter()
    adapter._start_time = _START
    event = _make_event(_START.replace(microsecond=200))

    assert adapter._is_stale_event(event) is True


def test_lxmf_pre_start_in_earlier_millisecond_is_stale() -> None:
    """LXMF rejects events from a strictly earlier millisecond.

    1 full second earlier — definitely in an earlier millisecond.
    """
    adapter = _make_lxmf_adapter()
    adapter._start_time = _START
    event = _make_event(_START - timedelta(seconds=1))

    assert adapter._is_stale_event(event) is True


def test_lxmf_post_start_in_same_millisecond_is_not_stale() -> None:
    """LXMF keeps post-start events within the startup millisecond.

    100µs after start, same millisecond.
    """
    adapter = _make_lxmf_adapter()
    adapter._start_time = _START
    event = _make_event(_START.replace(microsecond=900))

    assert adapter._is_stale_event(event) is False


def test_matrix_pre_start_in_same_millisecond_is_not_stale() -> None:
    """MatrixAdapter floors start to ms — events in same ms are not stale.

    600µs before start, same millisecond — ms floor prevents staleness.
    """
    adapter = _make_matrix_adapter()
    adapter._start_time = _START
    event = _make_event(_START.replace(microsecond=200), source_adapter="m")

    assert adapter._is_stale_event(event) is False


def test_matrix_pre_start_in_earlier_millisecond_is_stale() -> None:
    """MatrixAdapter rejects events from a strictly earlier millisecond.

    1 full second earlier — definitely in an earlier millisecond.
    """
    adapter = _make_matrix_adapter()
    adapter._start_time = _START
    event = _make_event(_START - timedelta(seconds=1), source_adapter="m")

    assert adapter._is_stale_event(event) is True


def test_contract_default_granularity_is_microsecond() -> None:
    """AdapterContract's default granularity preserves exact comparison."""
    assert AdapterContract._event_timestamp_granularity_us == 1
