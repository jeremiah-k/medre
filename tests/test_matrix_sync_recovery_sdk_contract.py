"""Installed mindroom-nio Classic Sync recovery contract guard."""

from __future__ import annotations

import importlib.metadata
import inspect

import pytest


@pytest.mark.matrix_sdk
def test_mindroom_nio_exposes_application_owned_classic_sync_contract() -> None:
    import nio
    from nio import event_provenance as provenance

    # Import name alone cannot distinguish mindroom-nio from upstream matrix-nio.
    assert importlib.metadata.version("mindroom-nio")

    config_params = inspect.signature(nio.AsyncClientConfig).parameters
    for name in (
        "max_timeouts",
        "backfill_limited_timelines",
        "store_sync_tokens",
        "backfill_persist_recovery",
        "replace_rotated_device_keys",
    ):
        assert name in config_params

    for name in (
        "add_event_admission_callback",
        "acknowledge_classic_sync",
        "acknowledge_unrecovered_rooms",
        "reset_classic_sync_state",
        "sync_forever",
        "stop_sync_forever",
        "to_device",
    ):
        assert callable(getattr(nio.AsyncClient, name, None))

    assert isinstance(
        getattr(nio.AsyncClient, "has_uncommitted_classic_sync_state", None),
        property,
    )
    admission_params = inspect.signature(
        nio.AsyncClient.add_event_admission_callback
    ).parameters
    assert len(admission_params) == 3  # self + callback + event classes
    assert callable(getattr(nio.events.MegolmEvent, "as_key_request", None))
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
