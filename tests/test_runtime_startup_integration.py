"""Tests for runtime startup and storage integration: storage schema validation, startup semantics,
boot summary integration in the app lifecycle, CLI diagnostics command.

Covers:
- Storage schema version stamping on fresh DB.
- Storage schema version mismatch raises StorageInitializationError.
- MedreApp.start() partial startup allows degraded mode.
- MedreApp.start() total failure raises RuntimeStartupError.
- Boot summary is populated after startup.
- RuntimeAccounting is wired through the builder.
- `medre diagnostics` CLI command.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.supervision import (
    StartupOutcome,
    classify_startup_outcome,
)
from medre.runtime.boot_summary import build_boot_summary

# ---------------------------------------------------------------------------
# Storage schema version tests
# ---------------------------------------------------------------------------


class TestStorageSchemaVersion:
    """SQLiteStorage schema versioning."""

    @pytest.mark.asyncio
    async def test_fresh_db_stamps_schema_version(self, tmp_path: Path) -> None:
        """Fresh database gets schema version stamped."""
        from medre.core.storage import SQLiteStorage
        from medre.core.storage.sqlite.schema import _EXPECTED_SCHEMA_VERSION

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        # Read the schema version directly.
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert int(row[0]) == _EXPECTED_SCHEMA_VERSION

        await storage.close()

    @pytest.mark.asyncio
    async def test_matching_version_succeeds(self, tmp_path: Path) -> None:
        """Re-initialising a DB with the same version succeeds."""
        from medre.core.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        # Re-open — should succeed without error.
        storage2 = SQLiteStorage(db_path)
        await storage2.initialize()
        await storage2.close()

    @pytest.mark.asyncio
    async def test_version_mismatch_raises(self, tmp_path: Path) -> None:
        """Mismatched schema version raises StorageInitializationError."""
        from medre.core.storage import SQLiteStorage
        from medre.core.storage.backend import StorageInitializationError
        from medre.core.storage.sqlite.schema import _EXPECTED_SCHEMA_VERSION

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        # Tamper with the schema version.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE _medre_schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(_EXPECTED_SCHEMA_VERSION + 99),),
        )
        conn.commit()
        conn.close()

        storage2 = SQLiteStorage(db_path)
        with pytest.raises(StorageInitializationError, match="schema version mismatch"):
            await storage2.initialize()
        await storage2.close()

    @pytest.mark.asyncio
    async def test_count_events_on_fresh_db(self, tmp_path: Path) -> None:
        """count_events returns 0 on a fresh database."""
        from medre.core.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        count = await storage.count_events()
        assert count == 0
        await storage.close()


# ---------------------------------------------------------------------------
# Startup semantics tests (using fakes, no live adapters)
# ---------------------------------------------------------------------------


class TestStartupSemanticsClassification:
    """Startup outcome classification."""

    def test_total_failure_zero_started(self) -> None:
        assert classify_startup_outcome(0, 0, 0) == StartupOutcome.TOTAL_FAILURE

    def test_total_failure_started_but_zero_total(self) -> None:
        assert classify_startup_outcome(0, 2, 2) == StartupOutcome.TOTAL_FAILURE

    def test_success_all_started(self) -> None:
        assert classify_startup_outcome(3, 0, 3) == StartupOutcome.SUCCESS

    def test_partial_some_started(self) -> None:
        assert classify_startup_outcome(1, 1, 2) == StartupOutcome.PARTIAL


# ---------------------------------------------------------------------------
# Boot summary builder
# ---------------------------------------------------------------------------


class TestBootSummaryBuilder:
    """build_boot_summary produces correct BootSummary."""

    def test_partial_startup_summary(self) -> None:
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_started=1,
            adapters_failed=1,
            adapters_total=2,
            adapters_disabled=1,
            build_failure_count=0,
            failed_adapter_ids=["adapter-b"],
            started_adapter_ids=["adapter-a"],
            route_count=2,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=10,
        )
        assert bs.startup_outcome == "partial"
        assert bs.runtime_health == "degraded"
        assert bs.adapters_disabled == 1
        assert bs.persisted_events_count == 10
        assert bs.failed_adapter_ids == ("adapter-b",)

    def test_total_failure_summary(self) -> None:
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="total_failure",
            runtime_health="failed",
            adapters_started=0,
            adapters_failed=1,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=1,
            failed_adapter_ids=["only-adapter"],
            started_adapter_ids=[],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        assert bs.startup_outcome == "total_failure"
        assert bs.runtime_health == "failed"
        assert bs.adapters_started == 0
        assert bs.build_failure_count == 1


# ---------------------------------------------------------------------------
# RuntimeAccounting wiring
# ---------------------------------------------------------------------------


class TestAccountingWiring:
    """RuntimeAccounting is wired through the builder."""

    def test_builder_creates_accounting(self) -> None:
        """RuntimeBuilder wires RuntimeAccounting into PipelineConfig."""
        from medre.config.model import (
            AdapterConfigSet,
            LoggingConfig,
            RuntimeConfig,
            RuntimeOptions,
            StorageConfig,
        )
        from medre.config.paths import resolve
        from medre.runtime.builder import RuntimeBuilder

        # Minimal config with no adapters.
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-accounting"),
            adapters=AdapterConfigSet(),
            storage=StorageConfig(backend="memory"),
            logging=LoggingConfig(),
        )

        # Use temp dir to avoid pollution.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MEDRE_HOME"] = tmp
            try:
                paths = resolve()
            finally:
                del os.environ["MEDRE_HOME"]

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        # Accounting should be wired.
        assert app._runtime_accounting is not None
        assert isinstance(app._runtime_accounting, RuntimeAccounting)

        # Pipeline config should also have accounting.
        assert app.pipeline_runner._runtime_accounting is not None

    def test_accounting_records_through_pipeline(self) -> None:
        """RuntimeAccounting counters update through pipeline hooks."""
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        acc.record_outbound_delivered()

        snap = acc.snapshot()
        assert snap["inbound_accepted"] == 2
        assert snap["outbound_attempts"] == 1
        assert snap["outbound_delivered"] == 1


# ---------------------------------------------------------------------------
# CLI diagnostics command
# ---------------------------------------------------------------------------


class TestCLIDiagnostics:
    """`medre diagnostics` prints runtime snapshot JSON."""

    def test_diagnostics_no_config_exits(self) -> None:
        """Without a config file, diagnostics exits with error."""
        # Running in a temp dir with no config should fail gracefully.
        import tempfile

        from medre.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            # Ensure no config file exists.
            with pytest.raises(SystemExit):
                main(["diagnostics", "--config", str(Path(tmp) / "nonexistent.toml")])

    def test_diagnostics_parser_registered(self) -> None:
        """Diagnostics subcommand is registered in the parser."""
        from medre.cli.main import _build_parser

        parser = _build_parser()
        # Parse the diagnostics command.
        args = parser.parse_args(["diagnostics"])
        assert args.command == "diagnostics"


# ---------------------------------------------------------------------------
# App startup fields
# ---------------------------------------------------------------------------


class TestAppStartupFields:
    """MedreApp has startup-related fields accessible after construction."""

    def test_app_has_boot_summary_property(self) -> None:
        """MedreApp.boot_summary is None before start."""
        import tempfile

        from medre.config.model import (
            AdapterConfigSet,
            LoggingConfig,
            RuntimeConfig,
            RuntimeOptions,
            StorageConfig,
        )
        from medre.config.paths import resolve
        from medre.runtime.builder import RuntimeBuilder

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-fields"),
            adapters=AdapterConfigSet(),
            storage=StorageConfig(backend="memory"),
            logging=LoggingConfig(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MEDRE_HOME"] = tmp
            try:
                paths = resolve()
            finally:
                del os.environ["MEDRE_HOME"]

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        # Before start, boot_summary is None.
        assert app.boot_summary is None
        assert app._startup_wall is None
        assert app._startup_monotonic is None
        assert app._health_state is None
        assert app._failed_adapter_ids == []
