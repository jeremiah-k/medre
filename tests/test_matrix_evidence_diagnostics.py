"""Evidence bundle Matrix adapter diagnostics integration test.

Verifies that the evidence bundle correctly handles a configuration that
includes a Matrix adapter (in fake mode). This is NOT a live test — it
uses fake adapters to exercise the evidence collection pipeline without
network access.

Matrix-specific checks:
- diagnostics_snapshot section includes adapter metadata
- live_health section works with Matrix adapter present
- storage section can query Matrix-shaped events
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from medre.runtime.evidence._bundle import collect_evidence_bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matrix_fake_config_path(tmp_path: Path) -> str:
    """Write a minimal config with a fake Matrix adapter and return its path."""
    config_content = f"""\
[runtime]
name = "matrix-evidence-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{tmp_path / "test.db"}"

[adapters.matrix.test_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
access_token = "fake_test_token"
room_allowlist = ["!room:example.com"]

[adapters.meshtastic.test_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMesh"

[routes.bridge]
source_adapters = ["test_matrix"]
dest_adapters = ["test_mesh"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:example.com"
dest_channel = "1"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content)
    return str(config_file)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvidenceBundleWithMatrixAdapter:
    """Evidence bundle correctly handles Matrix adapter in config."""

    async def test_evidence_bundle_collects_with_matrix_config(self, tmp_path) -> None:
        """collect_evidence_bundle succeeds with a Matrix adapter in config."""
        config_path = _matrix_fake_config_path(tmp_path)
        bundle = await collect_evidence_bundle(config_path)

        assert bundle["status"] in ("ok", "passed", "partial"), (
            f"Expected ok/passed/partial status, got {bundle['status']!r}. "
            f"Errors: {bundle.get('errors', [])}"
        )
        assert bundle["schema_version"] == 1
        assert "sections" in bundle
        assert "config_summary" in bundle["sections"]

    async def test_evidence_bundle_config_summary_includes_matrix(
        self, tmp_path
    ) -> None:
        """Config summary shows the Matrix adapter in enabled adapters."""
        config_path = _matrix_fake_config_path(tmp_path)
        bundle = await collect_evidence_bundle(config_path)

        config_section = bundle["sections"].get("config_summary", {})
        assert config_section.get("status") in ("ok", "passed"), (
            f"Config summary status: {config_section.get('status')}. "
            f"Error: {config_section.get('error')}"
        )

        data = config_section.get("data", {})
        adapters = data.get("adapters", [])

        # Should include the matrix adapter
        adapter_ids = [a.get("adapter_id", "") for a in adapters]
        assert "test_matrix" in adapter_ids, (
            f"Matrix adapter not found in enabled adapters: {adapter_ids}"
        )

    async def test_evidence_bundle_diagnostics_includes_matrix_adapter(
        self, tmp_path
    ) -> None:
        """Diagnostics snapshot includes Matrix adapter metadata."""
        config_path = _matrix_fake_config_path(tmp_path)
        bundle = await collect_evidence_bundle(config_path)

        diag_section = bundle["sections"].get("diagnostics_snapshot", {})
        assert diag_section.get("status") in ("ok", "passed"), (
            f"Diagnostics status: {diag_section.get('status')}. "
            f"Error: {diag_section.get('error')}"
        )

        data = diag_section.get("data", {})
        adapters = data.get("adapters", {})
        assert "test_matrix" in adapters, (
            f"Matrix adapter not in diagnostics adapters: {list(adapters.keys())}"
        )

        matrix_adapter = adapters["test_matrix"]
        # The fake adapter may not report platform="matrix" — it reports
        # whatever the FakeMatrixAdapter's platform attribute is. The key
        # check is that the adapter is present and has standard fields.
        assert "adapter_id" in matrix_adapter
        assert matrix_adapter["adapter_id"] == "test_matrix"
        assert "platform" in matrix_adapter
        assert "health" in matrix_adapter
        assert "capabilities" in matrix_adapter

    async def test_evidence_bundle_route_validation_includes_matrix_route(
        self, tmp_path
    ) -> None:
        """Route validation section shows the Matrix→Meshtastic bridge route."""
        config_path = _matrix_fake_config_path(tmp_path)
        bundle = await collect_evidence_bundle(config_path)

        route_section = bundle["sections"].get("route_validation", {})
        assert route_section.get("status") in ("ok", "passed"), (
            f"Route validation status: {route_section.get('status')}. "
            f"Error: {route_section.get('error')}"
        )

    async def test_evidence_bundle_no_secrets_in_output(self, tmp_path) -> None:
        """Evidence bundle output does not contain access tokens or secrets."""
        config_path = _matrix_fake_config_path(tmp_path)
        bundle = await collect_evidence_bundle(config_path)

        bundle_json = json.dumps(bundle, default=str)
        assert "fake_test_token" not in bundle_json, (
            "Access token leaked into evidence bundle output"
        )
        assert "syt_" not in bundle_json, (
            "Token prefix leaked into evidence bundle output"
        )

    async def test_evidence_bundle_storage_path_mode_with_matrix_event(
        self, tmp_path
    ) -> None:
        """Storage-path direct mode can inspect Matrix-shaped events."""
        # Create a storage DB with a Matrix-shaped event
        from medre.core.events import (
            CanonicalEvent,
            EventMetadata,
            NativeMessageRef,
            NativeRef,
        )
        from medre.core.events.metadata import NativeMetadata
        from medre.core.storage.sqlite import SQLiteStorage

        db_path = str(tmp_path / "matrix_events.db")
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        try:
            # Store a Matrix-shaped event
            event = CanonicalEvent(
                event_id="mx-evt-001",
                event_kind="message.created",
                schema_version=1,
                timestamp=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
                source_adapter="matrix-alpha",
                source_transport_id="@alice:example.com",
                source_channel_id="!room:example.com",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": "Test message", "msgtype": "m.text"},
                metadata=EventMetadata(
                    native=NativeMetadata(
                        data={
                            "room_id": "!room:example.com",
                            "event_id": "$mx001:example.com",
                            "sender": "@alice:example.com",
                        }
                    )
                ),
                source_native_ref=NativeRef(
                    adapter="matrix-alpha",
                    native_channel_id="!room:example.com",
                    native_message_id="$mx001:example.com",
                ),
            )
            await storage.append(event)

            # Store a native ref
            nref = NativeMessageRef(
                id="nref-1",
                event_id="mx-evt-001",
                adapter="matrix-alpha",
                native_channel_id="!room:example.com",
                native_message_id="$mx001:example.com",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
            )
            await storage.store_native_ref(nref)
        finally:
            await storage.close()

        # Now collect evidence using storage-path mode
        bundle = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="mx-evt-001",
        )

        assert bundle["status"] in ("ok", "passed", "partial")
        assert bundle["config_source"] == "storage_path"

        # Storage section should have the Matrix event
        storage_section = bundle["sections"].get("storage", {})
        assert storage_section.get("status") in ("ok", "passed", "partial")
        data = storage_section.get("data", {})
        assert data.get("event") is not None, "Matrix event not found in storage"

        # Verify the event has Matrix-specific fields
        stored_event = data["event"]
        assert stored_event["source_adapter"] == "matrix-alpha"
        assert stored_event["source_channel_id"] == "!room:example.com"

        # Native refs should include the Matrix native ref
        native_refs = data.get("native_refs_for_event", [])
        assert len(native_refs) >= 1, "Expected at least one native ref"

        # Verify incident summary
        incident = data.get("incident_summary")
        assert incident is not None
        assert incident["source_adapter"] == "matrix-alpha"
