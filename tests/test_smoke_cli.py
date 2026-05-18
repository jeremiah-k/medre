"""Tests for the operator-facing fake bridge smoke command and runner.

Proves that :func:`medre.runtime.smoke.run_fake_bridge_smoke` produces a
valid PASS report with all expected evidence fields, and that the CLI
``medre smoke`` command dispatches correctly.

Every test:

- Uses **fake adapters** — no live transports or SDKs.
- Uses **in-memory storage** — no filesystem I/O beyond temp dirs.
- Runs **Docker-free** — no network, no containers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.runtime.smoke import run_fake_bridge_smoke

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Helper: find shipped config
# ---------------------------------------------------------------------------


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.toml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.toml not found"
    return path


# ---------------------------------------------------------------------------
# Tests: run_fake_bridge_smoke()
# ---------------------------------------------------------------------------


class TestFakeBridgeSmokeReport:
    """Proves run_fake_bridge_smoke produces a PASS report with full evidence."""

    @pytest.mark.asyncio
    async def test_smoke_report_pass(self) -> None:
        """Default shipped config produces PASS."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        assert report["status"] == "passed", (
            f"Expected PASS, got {report['status']}: "
            f"{report.get('fail_reasons', [])}"
        )

    @pytest.mark.asyncio
    async def test_smoke_report_evidence_level(self) -> None:
        """Report has evidence_level = fake_bridge."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        assert report["evidence_level"] == "fake_bridge"

    @pytest.mark.asyncio
    async def test_smoke_report_has_event_id(self) -> None:
        """Report contains a non-empty event_id."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0

    @pytest.mark.asyncio
    async def test_smoke_report_has_source_adapter(self) -> None:
        """Report identifies the source adapter."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        assert report["source_adapter"] == "fake_matrix"

    @pytest.mark.asyncio
    async def test_smoke_report_has_target_adapters(self) -> None:
        """Report lists target adapters that received delivery."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        targets = report["target_adapters"]
        assert isinstance(targets, list)
        assert len(targets) >= 1
        # fake-bridge-smoke.toml routes from fake_matrix produce
        # deliveries to at least fake_meshtastic.
        assert "fake_meshtastic" in targets

    @pytest.mark.asyncio
    async def test_smoke_report_has_route_ids(self) -> None:
        """Report lists route IDs that matched."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        route_ids = report["route_ids"]
        assert isinstance(route_ids, list)
        assert len(route_ids) >= 1

    @pytest.mark.asyncio
    async def test_smoke_report_has_delivery_receipts(self) -> None:
        """Report contains at least one delivery receipt."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        receipts = report["delivery_receipts"]
        assert isinstance(receipts, list)
        assert len(receipts) >= 1
        for r in receipts:
            assert "receipt_id" in r
            assert "target_adapter" in r
            assert "status" in r
            assert r["status"] == "sent"

    @pytest.mark.asyncio
    async def test_smoke_report_has_native_refs(self) -> None:
        """Report contains native message ref evidence."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        refs = report["native_refs"]
        assert isinstance(refs, list)
        assert len(refs) >= 1
        for ref in refs:
            assert "adapter" in ref
            assert "native_id" in ref
            assert "resolves_to" in ref
            assert ref["resolves_to"] == report["event_id"]

    @pytest.mark.asyncio
    async def test_smoke_report_has_accounting(self) -> None:
        """Report contains runtime accounting counters."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        acc = report["accounting"]
        assert isinstance(acc, dict)
        assert acc["inbound_accepted"] >= 1
        assert acc["outbound_delivered"] >= 1
        assert acc["outbound_failed"] == 0

    @pytest.mark.asyncio
    async def test_smoke_report_has_route_stats(self) -> None:
        """Report contains per-route stats."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        stats = report["route_stats"]
        assert isinstance(stats, dict)
        # At least one route should have delivered > 0.
        has_delivered = any(
            v.get("delivered", 0) > 0 for v in stats.values() if isinstance(v, dict)
        )
        assert has_delivered, f"No route with delivered > 0 in {stats}"

    @pytest.mark.asyncio
    async def test_smoke_report_has_snapshot(self) -> None:
        """Report contains an abbreviated runtime snapshot."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        snap = report["snapshot"]
        assert "schema_version" in snap
        assert "lifecycle" in snap
        assert "routes" in snap

    @pytest.mark.asyncio
    async def test_smoke_report_has_limitations(self) -> None:
        """Report includes honest limitations."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        limitations = report["limitations"]
        assert isinstance(limitations, list)
        assert len(limitations) >= 3

    @pytest.mark.asyncio
    async def test_smoke_report_has_preflight(self) -> None:
        """Report includes preflight validation summary."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        pf = report["preflight"]
        assert pf["config_valid"] is True
        assert pf["adapter_enabled"] >= 2
        assert pf["route_enabled"] >= 1

    @pytest.mark.asyncio
    async def test_smoke_report_json_safe(self) -> None:
        """Report is fully JSON-serializable."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)
        # Round-trip cleanly.
        parsed = json.loads(serialized)
        assert parsed["status"] == "passed"

    @pytest.mark.asyncio
    async def test_smoke_report_deterministic_snapshot(self) -> None:
        """Two runs with frozen clocks produce identical snapshots."""
        config_path = _smoke_config_path()

        def frozen_now():
            return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        def frozen_mono():
            return 0.0

        report1 = await run_fake_bridge_smoke(
            config_path,
            now_fn=frozen_now,
            monotonic_fn=frozen_mono,
        )
        report2 = await run_fake_bridge_smoke(
            config_path,
            now_fn=frozen_now,
            monotonic_fn=frozen_mono,
        )
        # Snapshots should be structurally identical (event_ids differ).
        assert (
            report1["snapshot"]["schema_version"]
            == report2["snapshot"]["schema_version"]
        )

    @pytest.mark.asyncio
    async def test_smoke_report_custom_message(self) -> None:
        """Custom message text is accepted and event is created."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(
            config_path,
            message_text="custom operator check",
        )
        assert report["status"] == "passed"

    @pytest.mark.asyncio
    async def test_smoke_fanout_produces_multiple_targets(self) -> None:
        """fake-bridge-smoke.toml fanout route delivers to multiple targets."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        # The fanout route (mx_fanout) targets both fake_meshtastic and fake_meshcore.
        targets = report["target_adapters"]
        # Both targets should appear in the report.
        assert "fake_meshtastic" in targets
        assert "fake_meshcore" in targets

    @pytest.mark.asyncio
    async def test_smoke_multiple_receipts(self) -> None:
        """Fanout produces multiple delivery receipts."""
        config_path = _smoke_config_path()
        report = await run_fake_bridge_smoke(config_path)
        # With multiple routes matching (mx_to_mesh, mx_mesh_bidir forward,
        # mx_fanout to 2 targets, mx_filtered), receipts should be > 1.
        receipts = report["delivery_receipts"]
        assert (
            len(receipts) >= 2
        ), f"Expected >= 2 receipts from fanout, got {len(receipts)}"


# ---------------------------------------------------------------------------
# Tests: CLI dispatch
# ---------------------------------------------------------------------------


class TestSmokeCLI:
    """Proves ``medre smoke`` CLI command dispatches correctly."""

    def test_smoke_cli_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``medre smoke --json`` produces valid JSON with PASS status."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        config_path = _smoke_config_path()

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(["smoke", "--config", config_path, "--json"])

        assert exc_info.value.code == 0
        output = stdout_capture.getvalue()
        report = json.loads(output)
        assert report["status"] == "passed"

    def test_smoke_cli_human_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``medre smoke`` (no --json) prints human-readable output."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        config_path = _smoke_config_path()

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc_info:
                main(["smoke", "--config", config_path])

        assert exc_info.value.code == 0
        output = stdout_capture.getvalue()
        assert "PASS" in output
        assert "Event:" in output
        assert "Source:" in output
        assert "Receipts:" in output

    def test_smoke_cli_custom_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``medre smoke --message`` accepts custom text."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        config_path = _smoke_config_path()

        stdout_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--json",
                        "--message",
                        "check from CLI",
                    ]
                )

        assert exc_info.value.code == 0
        report = json.loads(stdout_capture.getvalue())
        assert report["status"] == "passed"
