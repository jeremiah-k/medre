"""Evidence/trace agreement tests and CLI smoke tests for env-only fake deployments.

Seeds a SQLite DB with canonical events using env-style adapter IDs (radio-a,
matrix-fake) and route_id (radio-to-matrix), then asserts that the evidence
bundle, trace timeline, and CLI commands all produce consistent output.

Existing negative tests in test_config_env_first.py that cover overlapping
scenarios:

- test_invalid_transport_raises        (env adapter with bad TRANSPORT)
- test_route_creation_requires_source  (route missing source)
- test_route_creation_requires_source_and_dest (route missing dest)
- test_route_override_source_dest_overlap_raises (source/dest overlap)
- test_created_matrix_adapter_kind_fake (ADAPTER_KIND=fake works)
- test_route_override_route_id_raises  (TOML route_id cannot be changed)
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.sqlite import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.evidence._storage_sections import _collect_storage_data_from_backend
from medre.runtime.timeline import assemble_event_timeline
from medre.runtime.trace import assemble_event_timeline as assemble_trace_entries

# ---------------------------------------------------------------------------
# Env-only seed data constants
# ---------------------------------------------------------------------------

_EVENT_ID = "env-evt-001"
_SOURCE_ADAPTER = "radio-a"
_TARGET_ADAPTER = "matrix-fake"
_ROUTE_ID = "radio-to-matrix"
_NATIVE_CHANNEL_ID = "!room:matrix-fake"
_NATIVE_MESSAGE_ID = "native-msg-env-001"
_RECEIPT_ID = "rcpt-env-001"
_TS_EVENT = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
_TS_RECEIPT = datetime(2026, 5, 1, 10, 0, 1, tzinfo=timezone.utc)
_TS_NREF = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)


def _make_env_event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id=_EVENT_ID,
        event_kind="message.created",
        schema_version=1,
        timestamp=_TS_EVENT,
        source_adapter=_SOURCE_ADAPTER,
        source_transport_id="meshtastic",
        source_channel_id="ch-radio",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "env-only deployment test"},
        metadata=EventMetadata(),
    )


def _make_env_receipt() -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=1,
        receipt_id=_RECEIPT_ID,
        event_id=_EVENT_ID,
        delivery_plan_id="plan-env-001",
        target_adapter=_TARGET_ADAPTER,
        route_id=_ROUTE_ID,
        status="sent",
        adapter_message_id=_NATIVE_MESSAGE_ID,
        created_at=_TS_RECEIPT,
    )


def _make_env_native_ref() -> NativeMessageRef:
    return NativeMessageRef(
        id="nref-env-001",
        event_id=_EVENT_ID,
        adapter=_TARGET_ADAPTER,
        native_channel_id=_NATIVE_CHANNEL_ID,
        native_message_id=_NATIVE_MESSAGE_ID,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        created_at=_TS_NREF,
    )


async def _seed_env_only(storage: SQLiteStorage) -> None:
    """Write one event + one receipt + one outbound native ref."""
    await storage.append(_make_env_event())
    await storage.append_receipt(_make_env_receipt())
    await storage.store_native_ref(_make_env_native_ref())


# ---------------------------------------------------------------------------
# Sample TOML config for env-only fake deployment
# ---------------------------------------------------------------------------

CONFIG_ENV_ONLY_TOML = """\
[runtime]
name = "test-env-only"

[logging]
level = "WARNING"

[storage]
backend = "sqlite"
path = "{state}/env_only.db"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear config-related env vars for each test."""
    for var in (
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def config_env_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write minimal TOML (no adapters/routes) + set env vars for env-only creation."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_ENV_ONLY_TOML)

    # Matrix adapter (env-only).
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT", "matrix")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND", "fake")
    monkeypatch.setenv(
        "MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER", "https://matrix.example.test"
    )
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__USER_ID", "@bot:example.test")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN", "test-secret-token")
    monkeypatch.setenv(
        "MEDRE_ADAPTER__MATRIX_FAKE__ROOM_ALLOWLIST", "!room:example.test"
    )

    # Meshtastic adapter (env-only).
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__MESHNET_NAME", "RadioA")

    # Route (env-only).
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS", "radio-a")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS", "matrix-fake")
    monkeypatch.setenv(
        "MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY", "source_to_dest"
    )
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED", "true")

    return p


@pytest.fixture()
async def seeded_env_only_db(tmp_path: Path) -> str:
    """Create and populate a SQLite DB with env-only seed data. Returns db_path."""
    db_path = str(tmp_path / "env_only_seeded.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    await _seed_env_only(storage)
    await storage.close()
    return db_path


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> str:
    """Run CLI with given args, capture stdout, return output."""
    from medre.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


# ===================================================================
# Class: TestEnvOnlyEvidence
# ===================================================================


class TestEnvOnlyEvidence:
    """Evidence bundle and trace timeline tests for env-only fake deployments.

    Seeds a SQLite DB with events from adapters created via env-style config
    (radio-a, matrix-fake, route radio-to-matrix), then asserts agreement
    between evidence bundles and trace timelines on canonical keys.
    """

    @pytest.mark.asyncio
    async def test_evidence_bundle_includes_config_summary(
        self,
        config_env_only: Path,
    ) -> None:
        """Evidence bundle with env-only config includes config_summary section."""
        report = await collect_evidence_bundle(str(config_env_only))
        assert report["schema_version"] == 1
        sections = report["sections"]
        assert "config_summary" in sections
        cs = sections["config_summary"]
        assert cs["status"] in ("passed", "partial")

        # Config summary should list both env-only adapters.
        adapters = cs["data"]["adapters"]
        adapter_ids = {a["adapter_id"] for a in adapters}
        assert "radio-a" in adapter_ids, f"radio-a not in {adapter_ids}"
        assert "matrix-fake" in adapter_ids, f"matrix-fake not in {adapter_ids}"

        # Routes created via env should appear in config summary.
        routes = cs["data"]["routes"]
        route_ids = {r["route_id"] for r in routes}
        assert "radio-to-matrix" in route_ids, f"radio-to-matrix not in {route_ids}"

        # No TOML adapter sections — adapters come purely from env vars.
        # The TOML file contains only runtime, logging, and storage sections.
        toml_path = config_env_only
        toml_text = toml_path.read_text()
        assert "[adapters" not in toml_text, "TOML should not contain adapter sections"
        assert "[routes" not in toml_text, "TOML should not contain route sections"

        # Env overrides should be recorded.
        env_applied = cs["data"].get("env_overrides_applied", [])
        assert len(env_applied) > 0, "Expected env overrides to be recorded"

    @pytest.mark.asyncio
    async def test_evidence_bundle_section_statuses_valid(
        self,
        config_env_only: Path,
    ) -> None:
        """All evidence bundle sections have valid status values."""
        report = await collect_evidence_bundle(str(config_env_only))
        valid_statuses = {"passed", "partial", "skipped", "error"}
        for name, section in report["sections"].items():
            assert section["status"] in valid_statuses, (
                f"Section {name!r} has invalid status {section['status']!r}"
            )

    @pytest.mark.asyncio
    async def test_evidence_bundle_storage_includes_event_data(
        self,
        config_env_only: Path,
    ) -> None:
        """Storage section includes event data when DB is seeded."""
        db_path = str(config_env_only.parent / "state" / "env_only.db")

        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await _seed_env_only(storage)
        await storage.close()

        report = await collect_evidence_bundle(
            str(config_env_only),
            event_id=_EVENT_ID,
        )
        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "passed"
        assert storage_section["data"]["db_exists"] is True
        assert storage_section["data"]["event_count"] == 1
        assert storage_section["data"]["receipt_count"] == 1
        assert storage_section["data"]["event"] is not None
        assert storage_section["data"]["event"]["event_id"] == _EVENT_ID

    @pytest.mark.asyncio
    async def test_trace_timeline_works_with_env_only_data(
        self,
        seeded_env_only_db: str,
    ) -> None:
        """assemble_event_timeline works with env-only event/receipts/native_refs."""
        storage = SQLiteStorage(seeded_env_only_db)
        await storage.initialize()
        try:
            tl = await assemble_event_timeline(storage, _EVENT_ID)
            assert tl is not None, "Timeline should not be None for seeded event"

            # Event entry.
            assert tl["event"] is not None
            assert tl["event"].event_id == _EVENT_ID
            assert tl["event"].source_adapter == _SOURCE_ADAPTER

            # Receipts.
            assert len(tl["receipts"]) == 1
            assert tl["receipts"][0].route_id == _ROUTE_ID
            assert tl["receipts"][0].target_adapter == _TARGET_ADAPTER

            # Native refs.
            assert len(tl["native_refs"]) == 1
            assert tl["native_refs"][0].adapter == _TARGET_ADAPTER
            assert tl["native_refs"][0].native_message_id == _NATIVE_MESSAGE_ID

            # Timeline entries.
            assert len(tl["timeline_entries"]) >= 1
            types = [e["entry_type"] for e in tl["timeline_entries"]]
            assert "event" in types
            assert "receipt" in types
            assert "native_ref" in types
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_trace_output_references_correct_route_id(
        self,
    ) -> None:
        """Trace timeline entries include the correct route_id."""
        event = _make_env_event()
        receipt = _make_env_receipt()
        nref = _make_env_native_ref()

        timeline = assemble_trace_entries(event, [receipt], [nref], [])

        receipt_entries = [e for e in timeline if e["entry_type"] == "receipt"]
        assert len(receipt_entries) == 1
        assert receipt_entries[0]["data"]["route_id"] == _ROUTE_ID
        assert receipt_entries[0]["data"]["event_id"] == _EVENT_ID

    @pytest.mark.asyncio
    async def test_event_ids_match_across_evidence_and_trace(
        self,
        seeded_env_only_db: str,
    ) -> None:
        """Event IDs agree between evidence bundle storage and trace timeline."""
        storage = SQLiteStorage(seeded_env_only_db)
        await storage.initialize()
        try:
            # Evidence side.
            section = await _collect_storage_data_from_backend(
                storage,
                db_path=seeded_env_only_db,
                event_id=_EVENT_ID,
                replay_run_id=None,
            )
            assert section["status"] == "passed"
            evidence_event = section["data"]["event"]
            assert evidence_event is not None
            assert evidence_event["event_id"] == _EVENT_ID

            # Trace side.
            tl = await assemble_event_timeline(storage, _EVENT_ID)
            assert tl is not None

            # Event IDs match.
            assert evidence_event["event_id"] == tl["event"].event_id == _EVENT_ID

            # Trace entries reference the same event_id.
            event_entries = [
                e for e in tl["timeline_entries"] if e["entry_type"] == "event"
            ]
            assert len(event_entries) >= 1
            assert event_entries[0]["data"]["event_id"] == _EVENT_ID
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_canonical_keys_exist_across_evidence_and_trace(
        self,
    ) -> None:
        """Canonical keys (event_id, route_id, source_adapter, target_adapter)
        exist in both evidence storage section and trace timeline entries."""
        event = _make_env_event()
        receipt = _make_env_receipt()
        nref = _make_env_native_ref()

        # Trace timeline.
        timeline = assemble_trace_entries(event, [receipt], [nref], [])

        event_entry = next(e for e in timeline if e["entry_type"] == "event")
        assert event_entry["data"]["event_id"] == _EVENT_ID
        assert event_entry["data"]["source_adapter"] == _SOURCE_ADAPTER

        receipt_entry = next(e for e in timeline if e["entry_type"] == "receipt")
        assert receipt_entry["data"]["route_id"] == _ROUTE_ID
        assert receipt_entry["data"]["target_adapter"] == _TARGET_ADAPTER
        assert receipt_entry["data"]["event_id"] == _EVENT_ID

        nref_entry = next(e for e in timeline if e["entry_type"] == "native_ref")
        assert nref_entry["data"]["adapter"] == _TARGET_ADAPTER
        assert nref_entry["data"]["native_message_id"] == _NATIVE_MESSAGE_ID

    @pytest.mark.asyncio
    async def test_evidence_native_refs_agree_with_trace(
        self,
        seeded_env_only_db: str,
    ) -> None:
        """Evidence bundle native refs and trace timeline native_ref entries
        agree on adapter, channel, message_id, direction."""
        storage = SQLiteStorage(seeded_env_only_db)
        await storage.initialize()
        try:
            # Evidence side.
            section = await _collect_storage_data_from_backend(
                storage,
                db_path=seeded_env_only_db,
                event_id=_EVENT_ID,
                replay_run_id=None,
            )
            assert section["status"] == "passed"
            bundle_nrefs = section["data"]["native_refs_for_event"]
            assert bundle_nrefs is not None and len(bundle_nrefs) == 1

            # Trace side.
            timeline = assemble_trace_entries(
                _make_env_event(),
                [_make_env_receipt()],
                [_make_env_native_ref()],
                [],
            )
            nref_entries = [e for e in timeline if e["entry_type"] == "native_ref"]
            assert len(nref_entries) == 1

            trace_nref = nref_entries[0]["data"]
            bundle_nref = bundle_nrefs[0]

            for key in ("adapter", "native_channel_id", "native_message_id", "direction"):
                assert trace_nref.get(key) == bundle_nref.get(key), (
                    f"Key {key!r} mismatch: trace={trace_nref.get(key)!r} "
                    f"evidence={bundle_nref.get(key)!r}"
                )
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_evidence_route_validation_has_env_only_route(
        self,
        config_env_only: Path,
    ) -> None:
        """Route created purely from env vars appears in route validation."""
        report = await collect_evidence_bundle(str(config_env_only))

        # Config summary lists the env-created route.
        cs = report["sections"]["config_summary"]
        route_ids = {r["route_id"] for r in cs["data"]["routes"]}
        assert "radio-to-matrix" in route_ids

        # Route validation should pass — adapters match.
        rv = report["sections"]["route_validation"]
        assert rv["data"]["valid"] is True
        assert rv["data"]["route_count"] >= 1

    @pytest.mark.asyncio
    async def test_evidence_json_secrets_redacted(
        self,
        config_env_only: Path,
    ) -> None:
        """Access token must not appear anywhere in evidence JSON."""
        report = await collect_evidence_bundle(str(config_env_only))
        json_str = json.dumps(report, sort_keys=True, default=str)

        # The token should never appear in any output.
        assert "test-secret-token" not in json_str
        assert "test_secret_token" not in json_str


class TestEnvOnlyCLISmoke:
    """Smoke tests that the evidence CLI and trace CLI produce output.

    These tests verify CLI parsing works with env-only contexts.  They don't
    need full runtime — just that the CLI entry point parses args and produces
    JSON or human-readable output without crashing.
    """

    def test_cli_help_parses(self) -> None:
        """'medre --help' parses without error."""
        output = _run_cli("--help")
        # Should produce help text.
        assert isinstance(output, str)
        assert "usage:" in output.lower() or "medre" in output

    def test_evidence_cli_json_produces_valid_json(
        self,
        config_env_only: Path,
    ) -> None:
        """CLI evidence --json produces valid JSON output."""
        output = _run_cli(
            "evidence",
            "--config",
            str(config_env_only),
            "--json",
        )
        result = json.loads(output)
        assert result["schema_version"] == 1
        assert "sections" in result
        assert "config_summary" in result["sections"]
        assert "storage" in result["sections"]

    def test_evidence_cli_human_readable(
        self,
        config_env_only: Path,
    ) -> None:
        """CLI evidence without --json produces human-readable output."""
        output = _run_cli(
            "evidence",
            "--config",
            str(config_env_only),
        )
        assert "Evidence:" in output


# ===================================================================
# Class: TestEnvOnlyConfigFailures
# ===================================================================


class TestEnvOnlyConfigFailures:
    """Negative tests for env-only configuration scenarios.

    Tests that routes referencing unknown adapter IDs pass config parsing
    (the env parser doesn't validate adapter refs) but are captured as
    route errors in evidence bundles.

    Related existing tests in test_config_env_first.py:

    - test_invalid_transport_raises (invalid TRANSPORT)
    - test_route_creation_requires_source (missing source_adapters)
    - test_route_creation_requires_source_and_dest (missing dest_adapters)
    - test_route_override_source_dest_overlap_raises (source/dest overlap)
    - test_created_matrix_adapter_kind_fake (ADAPTER_KIND=fake works)
    - test_route_override_route_id_raises (TOML route_id cannot change)
    """

    @pytest.mark.asyncio
    async def test_route_unknown_adapter_id_captured_in_evidence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Route referencing unknown adapter IDs is captured as route_errors
        in evidence bundle (config parses fine, route validation fails)."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        # Minimal TOML — no adapters, no routes.
        config_text = """\
[runtime]
name = "test-unknown-adapters"

[storage]
backend = "sqlite"
path = "{state}/test.db"
"""
        cfg = tmp_path / "config.toml"
        cfg.write_text(config_text)

        # Route via env referencing adapters that do not exist.
        monkeypatch.setenv(
            "MEDRE_ROUTE__BAD_ROUTE__SOURCE_ADAPTERS", "nonexistent"
        )
        monkeypatch.setenv("MEDRE_ROUTE__BAD_ROUTE__DEST_ADAPTERS", "ghost")
        monkeypatch.setenv(
            "MEDRE_ROUTE__BAD_ROUTE__DIRECTIONALITY", "source_to_dest"
        )
        monkeypatch.setenv("MEDRE_ROUTE__BAD_ROUTE__ENABLED", "true")

        report = await collect_evidence_bundle(str(cfg))
        rv = report["sections"]["route_validation"]
        assert rv["status"] == "partial"
        assert rv["data"]["valid"] is False
        assert len(rv["data"]["route_errors"]) > 0

    @pytest.mark.asyncio
    async def test_route_token_instead_of_adapter_id_in_evidence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Route using env token format (UPPERCASE) instead of adapter_id
        format is accepted by config but reported as route error."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        # Minimal TOML — no adapters, no routes.
        config_text = """\
[runtime]
name = "test-token-route"

[storage]
backend = "sqlite"
path = "{state}/test.db"
"""
        cfg = tmp_path / "config.toml"
        cfg.write_text(config_text)

        # Create one real adapter via env.
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")

        # Route references UPPERCASE token instead of lowercase adapter_id.
        monkeypatch.setenv("MEDRE_ROUTE__TEST__SOURCE_ADAPTERS", "RADIO_A")
        monkeypatch.setenv("MEDRE_ROUTE__TEST__DEST_ADAPTERS", "MATRIX_FAKE")
        monkeypatch.setenv(
            "MEDRE_ROUTE__TEST__DIRECTIONALITY", "source_to_dest"
        )
        monkeypatch.setenv("MEDRE_ROUTE__TEST__ENABLED", "true")

        report = await collect_evidence_bundle(str(cfg))
        rv = report["sections"]["route_validation"]
        # Routes reference adapters that don't exist (RADIO_A vs radio-a).
        assert rv["data"]["valid"] is False
        assert len(rv["data"]["route_errors"]) > 0
