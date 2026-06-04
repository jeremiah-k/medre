"""Evidence bundle Meshtastic adapter diagnostics integration test.

Verifies that the evidence bundle correctly handles a configuration that
includes a Meshtastic adapter (in fake mode).  This is NOT a live test — it
uses fake adapters to exercise the evidence collection pipeline without
radio hardware or network access.

Meshtastic-specific checks:
- diagnostics_snapshot section includes adapter metadata
- config_summary shows the Meshtastic adapter in enabled adapters
- storage section can query Meshtastic-shaped events
- no serial paths, host IPs, or BLE addresses leak into output
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


def _meshtastic_fake_config_path(tmp_path: Path) -> str:
    """Write a minimal config with fake Meshtastic adapters and return its path."""
    config_content = f"""\
[runtime]
name = "meshtastic-evidence-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{(tmp_path / "test.db").as_posix()}"

[adapters.meshtastic.test_mesh_a]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMeshA"

[adapters.meshtastic.test_mesh_b]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMeshB"

[routes.mesh_bridge]
source_adapters = ["test_mesh_a"]
dest_adapters = ["test_mesh_b"]
directionality = "source_to_dest"
enabled = true
dest_channel = "1"
"""
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_content, encoding="utf-8")
    return str(config_file)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvidenceBundleWithMeshtasticAdapter:
    """Evidence bundle correctly handles Meshtastic adapter in config."""

    _EVIDENCE_TIMEOUT = _EVIDENCE_TIMEOUT

    async def test_evidence_bundle_collects_with_meshtastic_config(
        self, tmp_path
    ) -> None:
        """collect_evidence_bundle succeeds with a Meshtastic adapter in config."""
        config_path = _meshtastic_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=self._EVIDENCE_TIMEOUT
        )

        assert bundle["status"] in ("ok", "passed", "partial"), (
            f"Expected ok/passed/partial status, got {bundle['status']!r}. "
            f"Errors: {bundle.get('errors', [])}"
        )
        assert bundle["schema_version"] == 1
        assert "sections" in bundle
        assert "config_summary" in bundle["sections"]

    async def test_evidence_bundle_config_summary_includes_meshtastic(
        self, tmp_path
    ) -> None:
        """Config summary shows the Meshtastic adapter in enabled adapters."""
        config_path = _meshtastic_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=self._EVIDENCE_TIMEOUT
        )

        config_section = bundle["sections"].get("config_summary", {})
        assert config_section.get("status") in ("ok", "passed"), (
            f"Config summary status: {config_section.get('status')}. "
            f"Error: {config_section.get('error')}"
        )

        data = config_section.get("data", {})
        adapters = data.get("adapters", [])

        adapter_ids = [a.get("adapter_id", "") for a in adapters]
        assert (
            "test_mesh_a" in adapter_ids
        ), f"Meshtastic adapter not found in enabled adapters: {adapter_ids}"
        assert (
            "test_mesh_b" in adapter_ids
        ), f"Second Meshtastic adapter not found in enabled adapters: {adapter_ids}"

    async def test_evidence_bundle_diagnostics_includes_meshtastic_adapter(
        self, tmp_path
    ) -> None:
        """Diagnostics snapshot includes Meshtastic adapter metadata."""
        config_path = _meshtastic_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=self._EVIDENCE_TIMEOUT
        )

        diag_section = bundle["sections"].get("diagnostics_snapshot", {})
        assert diag_section.get("status") in ("ok", "passed"), (
            f"Diagnostics status: {diag_section.get('status')}. "
            f"Error: {diag_section.get('error')}"
        )

        data = diag_section.get("data", {})
        adapters = data.get("adapters", {})
        assert (
            "test_mesh_a" in adapters
        ), f"Meshtastic adapter not in diagnostics adapters: {list(adapters.keys())}"

        mesh_adapter = adapters["test_mesh_a"]
        assert "adapter_id" in mesh_adapter
        assert mesh_adapter["adapter_id"] == "test_mesh_a"
        assert "platform" in mesh_adapter
        assert "health" in mesh_adapter
        assert "capabilities" in mesh_adapter

    async def test_evidence_bundle_route_validation_includes_meshtastic_route(
        self, tmp_path
    ) -> None:
        """Route validation section shows the Meshtastic route."""
        config_path = _meshtastic_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=self._EVIDENCE_TIMEOUT
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

        # Verify the Meshtastic route exists in config_summary
        config_section = bundle["sections"].get("config_summary", {})
        routes = config_section.get("data", {}).get("routes", [])
        assert any(
            "test_mesh_a" in r.get("source_adapters", [])
            and "test_mesh_b" in r.get("dest_adapters", [])
            for r in routes
        ), f"Expected Meshtastic route in config_summary routes: {routes}"

    async def test_evidence_bundle_no_secrets_in_output(self, tmp_path) -> None:
        """Evidence bundle output does not contain serial paths, host IPs, or secrets."""
        config_path = _meshtastic_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=self._EVIDENCE_TIMEOUT
        )

        bundle_json = json.dumps(bundle, default=str)
        # Meshtastic-specific: no serial port paths should leak
        assert (
            "/dev/ttyUSB" not in bundle_json
        ), "Serial port path leaked into evidence bundle output"
        assert (
            "/dev/ttyACM" not in bundle_json
        ), "Serial port path leaked into evidence bundle output"
        # No host/IP addresses should leak
        assert (
            "192.168." not in bundle_json
        ), "Host IP leaked into evidence bundle output"
        assert "10.0." not in bundle_json, "Host IP leaked into evidence bundle output"
        # No BLE MAC addresses should leak (AA:BB:CC:DD:EE:FF pattern)
        import re

        ble_mac = re.search(
            r"[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}",
            bundle_json,
        )
        assert (
            ble_mac is None
        ), f"Possible BLE MAC address leaked into evidence bundle: {ble_mac.group()}"

    async def test_evidence_bundle_storage_path_mode_with_meshtastic_event(
        self, tmp_path
    ) -> None:
        """Storage-path direct mode can inspect Meshtastic-shaped events."""
        from medre.core.events import (
            CanonicalEvent,
            EventMetadata,
            NativeMessageRef,
            NativeRef,
        )
        from medre.core.events.metadata import NativeMetadata
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "mesh_events.db")
        storage = SQLiteStorage(db_path=db_path)

        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id="mesh-evt-001",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_adapter="mesh-alpha",
                source_transport_id="!node1",
                source_channel_id="0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={
                    "body": "Test meshtastic message",
                    "portnum": "text_message",
                },
                metadata=EventMetadata(
                    native=NativeMetadata(
                        data={
                            "packet_id": 12345,
                            "from_id": "!node1",
                            "channel": 0,
                            "portnum": "text_message",
                            "to_id": "",
                            "is_direct_message": False,
                        }
                    )
                ),
                source_native_ref=NativeRef(
                    adapter="mesh-alpha",
                    native_channel_id="0",
                    native_message_id="12345",
                ),
            )
            await storage.append(event)

            nref = NativeMessageRef(
                id="nref-mesh-1",
                event_id="mesh-evt-001",
                adapter="mesh-alpha",
                native_channel_id="0",
                native_message_id="12345",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
            )
            await storage.store_native_ref(nref)
        finally:
            await storage.close()

        # Collect evidence using storage-path mode
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(
                storage_path=db_path,
                event_id="mesh-evt-001",
            ),
            timeout=self._EVIDENCE_TIMEOUT,
        )

        assert bundle["status"] in ("ok", "passed", "partial")
        assert bundle["config_source"] == "storage_path"

        # Storage section should have the Meshtastic event
        storage_section = bundle["sections"].get("storage", {})
        assert storage_section.get("status") in ("ok", "passed", "partial")
        data = storage_section.get("data", {})
        assert data.get("event") is not None, "Meshtastic event not found in storage"

        # Verify the event has Meshtastic-specific fields
        stored_event = data["event"]
        assert stored_event["source_adapter"] == "mesh-alpha"
        assert stored_event["source_channel_id"] == "0"

        # Native refs should include the Meshtastic native ref
        native_refs = data.get("native_refs_for_event", [])
        assert len(native_refs) >= 1, "Expected at least one native ref"

        # At least one native ref must match the stored Meshtastic ref fields
        match = any(
            nr.get("adapter") == "mesh-alpha"
            and nr.get("native_channel_id") == "0"
            and nr.get("native_message_id") == "12345"
            and nr.get("direction") == "inbound"
            for nr in native_refs
        )
        assert match, (
            f"No native ref matched expected Meshtastic inbound ref. "
            f"native_refs={native_refs}"
        )

        # Storage event should include source_native_ref or native metadata
        # with the Meshtastic packet_id
        assert stored_event.get("source_native_ref") is not None or (
            stored_event.get("metadata", {})
            .get("native", {})
            .get("data", {})
            .get("packet_id")
            == 12345
        ), "Storage event missing source_native_ref and native metadata packet_id"

        # Verify incident summary
        incident = data.get("incident_summary")
        assert incident is not None
        assert incident["source_adapter"] == "mesh-alpha"

    async def test_evidence_bundle_partial_works_without_live_hardware(
        self, tmp_path
    ) -> None:
        """Evidence bundle can collect without live hardware (fake mode only)."""
        config_path = _meshtastic_fake_config_path(tmp_path)
        bundle = await asyncio.wait_for(
            collect_evidence_bundle(config_path), timeout=self._EVIDENCE_TIMEOUT
        )

        # Must succeed (ok or partial) — no real radio required
        assert bundle["status"] in ("ok", "passed", "partial"), (
            f"Expected ok/passed/partial without hardware, got {bundle['status']!r}. "
            f"Errors: {bundle.get('errors', [])}"
        )

        # Diagnostics should report the fake adapter without hardware errors
        diag_section = bundle["sections"].get("diagnostics_snapshot", {})
        diag_errors = diag_section.get("error")
        assert (
            diag_errors is None or "hardware" not in str(diag_errors).lower()
        ), f"Unexpected hardware error in diagnostics: {diag_errors}"

        # Config summary should be clean
        config_section = bundle["sections"].get("config_summary", {})
        assert config_section.get("status") in (
            "ok",
            "passed",
        ), f"Config summary failed without hardware: {config_section.get('error')}"

        # No hardware-specific errors in top-level errors list
        for err in bundle.get("errors", []):
            assert "serial" not in err.lower(), f"Serial hardware error leaked: {err}"
            assert "ble" not in err.lower(), f"BLE hardware error leaked: {err}"
            assert (
                "tcp" not in err.lower() or "connection" not in err.lower()
            ), f"TCP connection error leaked: {err}"
