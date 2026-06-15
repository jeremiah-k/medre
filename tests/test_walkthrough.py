"""Walkthrough tests exercising operator-visible paths.

Uses run_fake_bridge_smoke (the function behind medre smoke) and
build_runtime_snapshot. For CLI handler-level tests that call
the command functions directly, see test_cli_config_and_smoke.py
and related CLI test files.

Retry and replay runtime integration tests are in
test_walkthrough_runtime_retry_replay.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from medre.config.loader import load_config
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.smoke import run_fake_bridge_smoke
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot
from medre.runtime.timeline import (
    assemble_event_timeline,
    assemble_storage_summary,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = str(_ROOT / "examples" / "configs" / "fake-bridge-smoke.yaml")


# ---------------------------------------------------------------------------
# Shared smoke report fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the smoke once for the whole module and return the report."""
    db_path = str(tmp_path_factory.mktemp("alpha") / "walkthrough.db")
    report = asyncio.run(run_fake_bridge_smoke(CONFIG_PATH, storage_path=db_path))
    assert report["status"] == "passed", (
        f"Smoke must pass before walkthrough tests can proceed: "
        f"{report.get('fail_reasons', [])}"
    )
    return report


# ===========================================================================
# Test 1: Config validates
# ===========================================================================


class TestConfigValidation:
    """Section 1.1: medre config check — config parses, adapters and routes
    validate."""

    def test_config_loads_without_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config, _source, _paths = load_config(CONFIG_PATH)
        assert config.runtime.name == "fake-bridge-smoke"

    def test_routes_validate_at_least_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config, _source, _paths = load_config(CONFIG_PATH)
        assert len(config.routes.routes) >= 1


# ===========================================================================
# Tests 2–5: Smoke, inspect, trace, evidence
# ===========================================================================


class TestSmokeInspectTrace:
    """Sections 1.2–1.4: smoke, inspect receipts, trace event, evidence."""

    def test_smoke_report_passed(self, smoke_report: dict[str, Any]) -> None:
        assert smoke_report["status"] == "passed"

    def test_smoke_event_id_present(self, smoke_report: dict[str, Any]) -> None:
        event_id: str = smoke_report["event_id"]
        assert isinstance(event_id, str) and len(event_id) > 0

    def test_smoke_receipt_count_at_least_one(
        self, smoke_report: dict[str, Any]
    ) -> None:
        receipts = smoke_report["delivery_receipts"]
        assert isinstance(receipts, list) and len(receipts) >= 1

    @pytest.mark.asyncio
    async def test_inspect_receipts_via_storage(
        self,
        smoke_report: dict[str, Any],
    ) -> None:
        """Storage API returns at least one receipt with status 'sent'."""
        storage_path = smoke_report["storage_path"]
        assert storage_path is not None

        from medre.core.storage.sqlite.storage import SQLiteStorage

        storage = SQLiteStorage(db_path=storage_path)
        try:
            await storage.initialize()
            event_id = smoke_report["event_id"]
            receipts = await storage.list_receipts_for_event(event_id)
            assert len(receipts) >= 1
            sent = [r for r in receipts if r.status == "sent"]
            assert len(sent) >= 1
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_trace_event_timeline(
        self,
        smoke_report: dict[str, Any],
    ) -> None:
        """assemble_event_timeline returns event with at least 1 receipt."""
        storage_path = smoke_report["storage_path"]
        assert storage_path is not None

        from medre.core.storage.sqlite.storage import SQLiteStorage

        storage = SQLiteStorage(db_path=storage_path)
        try:
            await storage.initialize()
            event_id = smoke_report["event_id"]
            timeline = await assemble_event_timeline(storage, event_id)
            assert timeline is not None
            assert timeline["event"] is not None
            assert len(timeline["receipts"]) >= 1
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_evidence_bundle(
        self,
        smoke_report: dict[str, Any],
    ) -> None:
        """assemble_storage_summary shows event_count >= 1, receipt_count >= 1."""
        storage_path = smoke_report["storage_path"]
        assert storage_path is not None

        from medre.core.storage.sqlite.storage import SQLiteStorage

        storage = SQLiteStorage(db_path=storage_path)
        try:
            await storage.initialize()
            summary = await assemble_storage_summary(storage)
            assert summary["event_count"] >= 1
            assert summary["receipt_count"] >= 1
        finally:
            await storage.close()


# ===========================================================================
# Test 8: Final snapshot
# ===========================================================================


class TestFinalSnapshot:
    """Build runtime snapshot and verify schema_version, accounting, lifecycle.

    The smoke report embeds a subset of the full runtime snapshot.  We verify
    the top-level contract: schema_version == 1 and accounting section present.
    The full lifecycle/runtime_state contract is verified separately by building
    a fresh runtime with retry disabled.
    """

    def test_snapshot_schema_version(self, smoke_report: dict[str, Any]) -> None:
        snap = smoke_report["snapshot"]
        assert snap["schema_version"] == SCHEMA_VERSION

    def test_snapshot_accounting_section_present(
        self, smoke_report: dict[str, Any]
    ) -> None:
        snap = smoke_report["snapshot"]
        assert "accounting" in snap

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_runtime_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """build_runtime_snapshot works with retry disabled, lifecycle section present."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config, _source, paths = load_config(CONFIG_PATH)
        app = RuntimeBuilder(config, paths).build()
        await app.start()
        try:
            snap = build_runtime_snapshot(app)
            assert snap["schema_version"] == 1
            assert "lifecycle" in snap
            assert snap["lifecycle"]["runtime_state"] in (
                "running",
                "initialized",
                "starting",
            )
            # Retry section exists with disabled defaults
            assert "retry" in snap
            assert snap["retry"]["enabled"] is False
            assert snap["retry"]["running"] is False
        finally:
            await app.stop()
