"""Installed mindroom-nio Classic Sync recovery contract guard."""

from __future__ import annotations

import inspect

import pytest


def test_mindroom_nio_exposes_application_owned_classic_sync_contract() -> None:
    nio = pytest.importorskip("nio")
    provenance = pytest.importorskip("nio.event_provenance")

    config_params = inspect.signature(nio.AsyncClientConfig).parameters
    for name in (
        "max_timeouts",
        "backfill_limited_timelines",
        "store_sync_tokens",
        "backfill_persist_recovery",
    ):
        assert name in config_params

    for name in (
        "add_event_admission_callback",
        "acknowledge_classic_sync",
        "reset_classic_sync_state",
        "sync_forever",
        "stop_sync_forever",
    ):
        assert callable(getattr(nio.AsyncClient, name, None))

    assert isinstance(
        getattr(nio.AsyncClient, "has_uncommitted_classic_sync_state", None),
        property,
    )
    assert list(provenance.TimelineEventProvenance) == [
        provenance.TimelineEventProvenance.LIVE,
        provenance.TimelineEventProvenance.RECOVERED,
        provenance.TimelineEventProvenance.HISTORY,
    ]
    assert [item.value for item in provenance.TimelineEventProvenance] == [
        "live",
        "recovered",
        "history",
    ]
