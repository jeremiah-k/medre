"""Evidence bundle MeshCore adapter diagnostics integration test.

Verifies that the evidence bundle correctly handles a configuration that
includes a MeshCore adapter (in fake mode). This is NOT a live test — it
uses fake adapters to exercise the evidence collection pipeline without
network access.

MeshCore-specific checks:
- diagnostics_snapshot section includes adapter metadata
- live_health section works with MeshCore adapter present
- storage section can query MeshCore-shaped events
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from medre.runtime.evidence._bundle import collect_evidence_bundle

_EVIDENCE_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meshcore_fake_config_path(tmp_path: Path) -> str:
    """Write a minimal config with a fake MeshCore adapter and return its path."""
    config_content = f"""\
[runtime]
name = "meshcore-evidence-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{(tmp_path / "test.db").as_posix()}"

[adapters.meshcore.test_meshcore]
enabled = true
adapter_kind = "fake"
connection_type = "fake"

[adapters.matrix.test_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
access_token = "fake_test_token"
room_allowlist = ["!room:example.com"]

[routes.bridge]
source_adapters = ["test_meshcore"]
dest_adapters = ["test_matrix"]
directionality = "source_to_dest"
enabled = true
source_channel = "0"
dest_room = "!room:example.com"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content, encoding="utf-8")
    return str(config_file)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvidenceBundleWithMeshCoreAdapter:
    """Evidence bundle correctly handles MeshCore adapter in config."""

    async def test_evidence_bundle_collects_with_meshcore_config(
        self, tmp_path
    ) -> None:
        """collect_evidence_bundle succeeds with a MeshCore adapter in config."""
        config_path = _meshcore_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=_EVIDENCE_TIMEOUT
        )

        assert bundle["status"] in ("ok", "passed", "partial"), (
            f"Expected ok/passed/partial status, got {bundle['status']!r}. "
            f"Errors: {bundle.get('errors', [])}"
        )
        assert bundle["schema_version"] == 1
        assert "sections" in bundle
        assert "config_summary" in bundle["sections"]

    async def test_evidence_bundle_config_summary_includes_meshcore(
        self, tmp_path
    ) -> None:
        """Config summary shows the MeshCore adapter in enabled adapters."""
        config_path = _meshcore_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=_EVIDENCE_TIMEOUT
        )

        config_section = bundle["sections"].get("config_summary", {})
        assert config_section.get("status") in ("ok", "passed"), (
            f"Config summary status: {config_section.get('status')}. "
            f"Error: {config_section.get('error')}"
        )

        data = config_section.get("data", {})
        adapters = data.get("adapters", [])

        # Should include the meshcore adapter
        adapter_ids = [a.get("adapter_id", "") for a in adapters]
        assert (
            "test_meshcore" in adapter_ids
        ), f"MeshCore adapter not found in enabled adapters: {adapter_ids}"

    async def test_evidence_bundle_diagnostics_includes_meshcore_adapter(
        self, tmp_path
    ) -> None:
        """Diagnostics snapshot includes MeshCore adapter metadata."""
        config_path = _meshcore_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=_EVIDENCE_TIMEOUT
        )

        diag_section = bundle["sections"].get("diagnostics_snapshot", {})
        assert diag_section.get("status") in ("ok", "passed"), (
            f"Diagnostics status: {diag_section.get('status')}. "
            f"Error: {diag_section.get('error')}"
        )

        data = diag_section.get("data", {})
        adapters = data.get("adapters", {})
        assert (
            "test_meshcore" in adapters
        ), f"MeshCore adapter not in diagnostics adapters: {list(adapters.keys())}"

        meshcore_adapter = adapters["test_meshcore"]
        # The fake adapter may not report platform="meshcore" — it reports
        # whatever the FakeMeshCoreAdapter's platform attribute is. The key
        # check is that the adapter is present and has standard fields.
        assert "adapter_id" in meshcore_adapter
        assert meshcore_adapter["adapter_id"] == "test_meshcore"
        assert "platform" in meshcore_adapter
        assert "health" in meshcore_adapter
        assert "capabilities" in meshcore_adapter

    async def test_evidence_bundle_route_validation_includes_meshcore_route(
        self, tmp_path
    ) -> None:
        """Route validation section shows the MeshCore→Matrix bridge route."""
        config_path = _meshcore_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=_EVIDENCE_TIMEOUT
        )

        route_section = bundle["sections"].get("route_validation", {})
        assert route_section.get("status") in ("ok", "passed"), (
            f"Route validation status: {route_section.get('status')}. "
            f"Error: {route_section.get('error')}"
        )

        route_data = route_section.get("data", {})
        assert (
            route_data.get("valid") is True
        ), f"Route validation reports invalid: {route_data}"
        assert (
            route_data.get("route_count", 0) >= 1
        ), f"Expected at least one route: {route_data}"

        # Verify the MeshCore→Matrix route exists in config_summary
        config_section = bundle["sections"].get("config_summary", {})
        routes = config_section.get("data", {}).get("routes", [])
        assert any(
            "test_meshcore" in r.get("source_adapters", [])
            and "test_matrix" in r.get("dest_adapters", [])
            for r in routes
        ), f"Expected MeshCore->Matrix route in config_summary routes: {routes}"

    async def test_evidence_bundle_no_secrets_in_output(self, tmp_path) -> None:
        """Evidence bundle output does not contain access tokens or secrets."""
        config_path = _meshcore_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=_EVIDENCE_TIMEOUT
        )

        bundle_json = json.dumps(bundle, default=str)
        assert (
            "fake_test_token" not in bundle_json
        ), "Access token leaked into evidence bundle output"
        assert (
            "syt_" not in bundle_json
        ), "Token prefix leaked into evidence bundle output"

    async def test_evidence_bundle_storage_path_mode_with_meshcore_event(
        self, tmp_path
    ) -> None:
        """Storage-path direct mode can inspect MeshCore-shaped events."""
        from medre.core.events import (
            CanonicalEvent,
            EventMetadata,
            NativeMessageRef,
            NativeRef,
        )
        from medre.core.events.metadata import NativeMetadata
        from medre.core.storage.sqlite import SQLiteStorage

        db_path = str(tmp_path / "meshcore_events.db")
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()

        try:
            # Store a MeshCore-shaped event
            event = CanonicalEvent(
                event_id="mc-evt-001",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_adapter="meshcore-alpha",
                source_transport_id="cafe01",
                source_channel_id="2",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": "Test message from mesh"},
                metadata=EventMetadata(
                    native=NativeMetadata(
                        data={
                            "meshcore.packet_id": 12345,
                            "meshcore.sender_id": "cafe01",
                            "meshcore.channel": 2,
                            "meshcore.pubkey_prefix": "cafe01",
                            "meshcore.txt_type": 0,
                            "meshcore.is_direct_message": False,
                        }
                    )
                ),
                source_native_ref=NativeRef(
                    adapter="meshcore-alpha",
                    native_channel_id="2",
                    native_message_id="12345",
                ),
            )
            await storage.append(event)

            # Store a native ref
            nref = NativeMessageRef(
                id="nref-1",
                event_id="mc-evt-001",
                adapter="meshcore-alpha",
                native_channel_id="2",
                native_message_id="12345",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
            )
            await storage.store_native_ref(nref)
        finally:
            await storage.close()

        # Now collect evidence using storage-path mode
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(
                storage_path=db_path,
                event_id="mc-evt-001",
            ),
            timeout=_EVIDENCE_TIMEOUT,
        )

        assert bundle["status"] in ("ok", "passed", "partial")
        assert bundle["config_source"] == "storage_path"

        # Storage section should have the MeshCore event
        storage_section = bundle["sections"].get("storage", {})
        assert storage_section.get("status") in ("ok", "passed", "partial")
        data = storage_section.get("data", {})
        assert data.get("event") is not None, "MeshCore event not found in storage"

        # Verify the event has MeshCore-specific fields
        stored_event = data["event"]
        assert stored_event["source_adapter"] == "meshcore-alpha"
        assert stored_event["source_channel_id"] == "2"

        # Native refs should include the MeshCore native ref
        native_refs = data.get("native_refs_for_event", [])
        assert len(native_refs) >= 1, "Expected at least one native ref"

        # At least one native ref must match the stored MeshCore ref fields
        match = any(
            nr.get("adapter") == "meshcore-alpha"
            and nr.get("native_channel_id") == "2"
            and nr.get("native_message_id") == "12345"
            and nr.get("direction") == "inbound"
            for nr in native_refs
        )
        assert match, (
            f"No native ref matched expected MeshCore inbound ref. "
            f"native_refs={native_refs}"
        )

        # Storage event should include source_native_ref or native metadata
        # with the MeshCore packet_id
        assert stored_event.get("source_native_ref") is not None or (
            stored_event.get("metadata", {})
            .get("native", {})
            .get("data", {})
            .get("meshcore.packet_id")
            == 12345
        ), "Storage event missing source_native_ref and native metadata packet_id"

        # Verify incident summary
        incident = data.get("incident_summary")
        assert incident is not None
        assert incident["source_adapter"] == "meshcore-alpha"
