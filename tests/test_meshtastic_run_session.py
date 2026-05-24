"""Meshtastic RuntimeBuilder pipeline smoke test (not operator CLI run-session).

Builds a runtime with two fake Meshtastic adapters (mesh_source → mesh_dest),
injects a text event through the pipeline, and verifies end-to-end routing,
storage receipts, and native refs — all without live radio or Docker.

Uses RuntimeBuilder directly (not run_fake_bridge_smoke) because the
Meshtastic-only config has no Matrix adapter, and the smoke runner's
source-adapter picker prefers Matrix adapters and falls back to
alphabetical order, which would pick the wrong source adapter.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from medre.config.loader import load_config
from medre.core.events.canonical import CanonicalEvent
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Config template
# ---------------------------------------------------------------------------

_MESHTASTIC_SESSION_CONFIG = """\
[runtime]
name = "meshtastic-run-session"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{db_path}"

[adapters.meshtastic.mesh_source]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMesh"

[adapters.meshtastic.mesh_dest]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMesh"

[routes.mesh_to_mesh]
source_adapters = ["mesh_source"]
dest_adapters = ["mesh_dest"]
directionality = "source_to_dest"
enabled = true
dest_channel = "0"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, db_path: str) -> Path:
    """Write the Meshtastic-only config TOML and return its path."""
    config_path = tmp_path / "meshtastic_session.toml"
    config_path.write_text(_MESHTASTIC_SESSION_CONFIG.format(db_path=db_path))
    return config_path


def _make_pipeline_event(adapter: Any, text: str) -> CanonicalEvent:
    """Create a CanonicalEvent with both 'body' and 'text' payload keys.

    FakeMeshtasticAdapter.make_text_event decodes a Meshtastic packet which
    stores the text under ``payload["body"]``.  TextRenderer reads
    ``payload["text"]``.  This helper bridges the gap so the rendered output
    is non-empty.
    """
    base = adapter.make_text_event(body=text)
    merged = dict(base.payload)
    merged["text"] = text
    return CanonicalEvent(
        event_id=base.event_id,
        event_kind=base.event_kind,
        schema_version=base.schema_version,
        timestamp=base.timestamp,
        source_adapter=base.source_adapter,
        source_transport_id=base.source_transport_id,
        source_channel_id=base.source_channel_id,
        parent_event_id=base.parent_event_id,
        lineage=base.lineage,
        relations=base.relations,
        payload=merged,
        metadata=base.metadata,
        source_native_ref=base.source_native_ref,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestMeshtasticRunSession:
    """End-to-end smoke test: fake Meshtastic source → fake Meshtastic dest.

    Proves that a runtime session with Meshtastic-only adapters builds,
    starts, routes events, and persists storage receipts and native refs.
    """

    @pytest.mark.asyncio
    async def test_meshtastic_run_session_smoke(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "session.db")
        config_path = _write_config(tmp_path, db_path)

        config, _source, paths = load_config(str(config_path))

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        try:
            await asyncio.wait_for(
                _run_session(app, db_path),
                timeout=30.0,
            )
        finally:
            try:
                await app.stop()
            except Exception:
                pass


async def _run_session(app: Any, db_path: str) -> None:
    """Drive a single event through the pipeline and verify evidence."""
    await app.start()

    # -- Pick source adapter --------------------------------------------------
    source_adapter = app.adapters["mesh_source"]

    # -- Create and inject event ----------------------------------------------
    event = _make_pipeline_event(source_adapter, "meshtastic run-session smoke")

    outcomes = await app.pipeline_runner.handle_ingress(event)

    # -- Verify delivery outcomes ---------------------------------------------
    assert len(outcomes) >= 1, "Expected at least one delivery outcome"
    successful = [o for o in outcomes if o.status == "success"]
    assert (
        len(successful) >= 1
    ), f"No successful deliveries; outcomes: {[o.status for o in outcomes]}"

    target = successful[0].target_adapter
    assert target == "mesh_dest"

    # -- Verify storage: event persisted --------------------------------------
    storage = app.storage
    assert storage is not None

    stored_event = await storage.get(event.event_id)
    assert stored_event is not None, f"Event {event.event_id!r} not found in storage"
    assert stored_event.event_id == event.event_id

    # -- Verify storage: delivery receipts ------------------------------------
    receipts = await storage.list_receipts_for_event(event.event_id)
    assert len(receipts) >= 1, "Expected at least one delivery receipt"
    sent_receipts = [r for r in receipts if r.status == "sent"]
    assert len(sent_receipts) >= 1, "No receipt with status 'sent'"

    # -- Verify storage: native refs ------------------------------------------
    native_refs = await storage.list_native_refs_for_event(event.event_id)
    outbound_refs = [
        nr for nr in native_refs if getattr(nr, "direction", None) == "outbound"
    ]
    assert (
        len(outbound_refs) >= 1
    ), "Expected at least one outbound native ref for mesh_dest"

    # The FakeMeshtasticAdapter generates sequential packet IDs starting at 1.
    dest_ref = outbound_refs[0]
    assert getattr(dest_ref, "adapter", None) == "mesh_dest"
    assert getattr(dest_ref, "native_message_id", None) is not None

    # Resolve the outbound native ref back to the event.
    resolved = await storage.resolve_native_ref(
        adapter="mesh_dest",
        native_channel_id=dest_ref.native_channel_id,
        native_message_id=dest_ref.native_message_id,
    )
    assert (
        resolved == event.event_id
    ), f"Native ref resolved to {resolved!r}, expected {event.event_id!r}"

    # -- Verify db file exists ------------------------------------------------
    assert Path(db_path).exists(), f"Storage DB file not found at {db_path}"
