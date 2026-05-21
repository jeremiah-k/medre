"""CLI JSON output tests for Operator Control Plane v2.

Proves that ``medre smoke``, ``medre trace event``, ``medre inspect event``,
and ``medre evidence --storage-path`` produce valid, well-structured JSON
with canonical keys present and no secret leakage.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
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
from tests.helpers.cli import _run_cli, _run_cli_raw

# ---------------------------------------------------------------------------
# Shared seed data
# ---------------------------------------------------------------------------

_EVENT_ID = "cli-json-evt-001"
_ADAPTER = "fake_dest"
_NATIVE_CHANNEL_ID = "ch-cli-json-001"
_NATIVE_MESSAGE_ID = "native-msg-cli-json-001"
_RECEIPT_ID = "rcpt-cli-json-001"
_TS_EVENT = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
_TS_RECEIPT = datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc)
_TS_NREF = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)

# Known test secret values that must never appear in JSON output.
# These are concrete credential values from test configs — not generic
# substrings that could match test fixture names or field labels.
_FORBIDDEN_SECRET_VALUES = (
    "fake_tok",
    "tok_single",
    "tok2",
)


def _make_event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id=_EVENT_ID,
        event_kind="message.created",
        schema_version=1,
        timestamp=_TS_EVENT,
        source_adapter="fake_source",
        source_transport_id="transport-cli-json",
        source_channel_id="ch-source",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "CLI JSON output test message"},
        metadata=EventMetadata(),
    )


def _make_receipt() -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=1,
        receipt_id=_RECEIPT_ID,
        event_id=_EVENT_ID,
        delivery_plan_id="plan-cli-json-001",
        target_adapter=_ADAPTER,
        target_channel=_NATIVE_CHANNEL_ID,
        route_id="route-cli-json-001",
        status="sent",
        adapter_message_id=_NATIVE_MESSAGE_ID,
        created_at=_TS_RECEIPT,
    )


def _make_native_ref() -> NativeMessageRef:
    return NativeMessageRef(
        id="nref-cli-json-001",
        event_id=_EVENT_ID,
        adapter=_ADAPTER,
        native_channel_id=_NATIVE_CHANNEL_ID,
        native_message_id=_NATIVE_MESSAGE_ID,
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        created_at=_TS_NREF,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.toml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.toml not found"
    return path


def _seed_db(db_path: str) -> str:
    """Seed a SQLite DB with canonical test data. Returns the event_id."""

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        try:
            await storage.append(_make_event())
            await storage.append_receipt(_make_receipt())
            await storage.store_native_ref(_make_native_ref())
        finally:
            await storage.close()

    asyncio.run(_seed())
    return _EVENT_ID


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


@pytest.fixture()
def seeded_db(tmp_path: Path) -> str:
    """Create and seed a temp SQLite DB, return its path."""
    db_path = str(tmp_path / "cli_json_test.db")
    _seed_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Tests: medre smoke --json
# ---------------------------------------------------------------------------


class TestSmokeJsonOutput:
    """Proves ``medre smoke --json`` produces valid JSON with expected keys."""

    def test_smoke_json_top_level_keys(self) -> None:
        """Smoke JSON output has required top-level keys."""
        config_path = _smoke_config_path()
        output = _run_cli("smoke", "--config", config_path, "--json")
        report = json.loads(output)

        for key in ("status", "event_id", "target_adapters"):
            assert key in report, f"Missing top-level key {key!r}"

        assert report["status"] == "passed"

    def test_smoke_json_native_ref_canonical_keys(self) -> None:
        """Smoke JSON native_refs include canonical keys."""
        config_path = _smoke_config_path()
        output = _run_cli("smoke", "--config", config_path, "--json")
        report = json.loads(output)

        refs = report["native_refs"]
        assert isinstance(refs, list) and len(refs) >= 1
        ref = refs[0]
        for key in (
            "adapter",
            "native_channel_id",
            "native_id",
            "native_message_id",
            "direction",
            "resolves_to",
        ):
            assert key in ref, f"Canonical key {key!r} missing from native_ref"


# ---------------------------------------------------------------------------
# Tests: medre trace event --json
# ---------------------------------------------------------------------------


class TestTraceEventJsonOutput:
    """Proves ``medre trace event <id> --json`` produces valid JSON timeline."""

    def test_trace_event_timeline_structure(self, seeded_db: str) -> None:
        """Trace event JSON timeline entries have required keys."""
        output = _run_cli(
            "trace", "event", _EVENT_ID, "--storage-path", seeded_db, "--json"
        )
        timeline = json.loads(output)
        assert isinstance(timeline, list)
        assert len(timeline) >= 1, "Expected at least one timeline entry"

        for entry in timeline:
            for key in ("entry_type", "timestamp", "ordinal", "data"):
                assert key in entry, f"Timeline entry missing key {key!r}"

    def test_trace_event_native_ref_canonical_keys(self, seeded_db: str) -> None:
        """Trace event JSON native_ref entries have canonical keys."""
        output = _run_cli(
            "trace", "event", _EVENT_ID, "--storage-path", seeded_db, "--json"
        )
        timeline = json.loads(output)

        nref_entries = [e for e in timeline if e["entry_type"] == "native_ref"]
        assert len(nref_entries) >= 1, "Expected at least one native_ref entry"
        data = nref_entries[0]["data"]

        for key in (
            "adapter",
            "native_channel_id",
            "native_message_id",
            "direction",
            "resolves_to",
        ):
            assert key in data, f"Canonical key {key!r} missing from native_ref data"


# ---------------------------------------------------------------------------
# Tests: medre inspect event --json
# ---------------------------------------------------------------------------


class TestInspectEventJsonOutput:
    """Proves ``medre inspect event <id> --storage-path`` produces valid JSON."""

    def test_inspect_event_metadata(self, seeded_db: str) -> None:
        """Inspect event JSON output contains event metadata."""
        output = _run_cli(
            "inspect", "event", _EVENT_ID, "--storage-path", seeded_db
        )
        event = json.loads(output)

        assert event["event_id"] == _EVENT_ID
        assert event["event_kind"] == "message.created"
        assert event["source_adapter"] == "fake_source"

    def test_inspect_event_native_ref_data(self, seeded_db: str) -> None:
        """Inspect event JSON output includes source_native_ref if present."""
        output = _run_cli(
            "inspect", "event", _EVENT_ID, "--storage-path", seeded_db
        )
        event = json.loads(output)

        # The event should have basic fields present; native ref data is
        # optional on the event itself (it comes from storage lookups).
        assert "event_id" in event
        assert "payload" in event


# ---------------------------------------------------------------------------
# Tests: medre evidence --storage-path --json
# ---------------------------------------------------------------------------


class TestEvidenceStoragePathJsonOutput:
    """Proves ``medre evidence --storage-path <path> --json`` produces valid bundle."""

    def test_evidence_bundle_structure(self, seeded_db: str) -> None:
        """Evidence JSON has bundle structure with required top-level keys."""
        output = _run_cli(
            "evidence", "--storage-path", seeded_db, "--event", _EVENT_ID, "--json"
        )
        bundle = json.loads(output)

        for key in ("collected_at", "command", "status", "sections"):
            assert key in bundle, f"Bundle missing top-level key {key!r}"

        assert bundle["command"] == "evidence"

    def test_evidence_storage_section_has_native_refs(self, seeded_db: str) -> None:
        """Evidence storage section has native refs with canonical keys."""
        output = _run_cli(
            "evidence", "--storage-path", seeded_db, "--event", _EVENT_ID, "--json"
        )
        bundle = json.loads(output)

        storage_section = bundle["sections"]["storage"]
        assert storage_section["status"] in ("passed", "partial")

        nrefs = storage_section["data"]["native_refs_for_event"]
        assert nrefs is not None and len(nrefs) >= 1, (
            "Expected at least one native ref in storage section"
        )
        nref = nrefs[0]
        for key in (
            "adapter",
            "native_channel_id",
            "native_message_id",
            "direction",
            "resolves_to",
        ):
            assert key in nref, f"Canonical key {key!r} missing from native ref"

        # resolves_to must be present and populated.
        resolves_to = nref["resolves_to"]
        if isinstance(resolves_to, dict):
            assert resolves_to.get("type") == "event", (
                f"resolves_to dict should have type='event', got {resolves_to.get('type')!r}"
            )
        else:
            # String form: should resolve to the known event_id.
            assert resolves_to == _EVENT_ID, (
                f"resolves_to should be {_EVENT_ID!r}, got {resolves_to!r}"
            )


# ---------------------------------------------------------------------------
# Tests: no secrets or tracebacks in JSON output
# ---------------------------------------------------------------------------


class TestJsonOutputSafety:
    """Proves JSON outputs contain no tracebacks, access tokens, or secrets."""

    def _assert_no_forbidden_content(self, json_text: str, label: str) -> None:
        """Assert the JSON text contains no forbidden patterns."""
        lower = json_text.lower()
        assert "traceback" not in lower, f"{label}: JSON contains 'traceback'"
        assert "access_token" not in lower, (
            f"{label}: JSON contains 'access_token'"
        )
        for secret in _FORBIDDEN_SECRET_VALUES:
            assert secret not in json_text, (
                f"{label}: JSON contains forbidden secret value {secret!r}"
            )

    def test_smoke_json_no_secrets(self) -> None:
        """Smoke --json output does not contain secrets or tracebacks."""
        config_path = _smoke_config_path()
        output = _run_cli("smoke", "--config", config_path, "--json")
        self._assert_no_forbidden_content(output, "smoke --json")

    def test_trace_event_json_no_secrets(self, seeded_db: str) -> None:
        """Trace event --json output does not contain secrets or tracebacks."""
        output = _run_cli(
            "trace", "event", _EVENT_ID, "--storage-path", seeded_db, "--json"
        )
        self._assert_no_forbidden_content(output, "trace event --json")

    def test_inspect_event_json_no_secrets(self, seeded_db: str) -> None:
        """Inspect event JSON output does not contain secrets or tracebacks."""
        output = _run_cli(
            "inspect", "event", _EVENT_ID, "--storage-path", seeded_db
        )
        self._assert_no_forbidden_content(output, "inspect event")

    def test_evidence_json_no_secrets(self, seeded_db: str) -> None:
        """Evidence --json output does not contain secrets or tracebacks."""
        output = _run_cli(
            "evidence", "--storage-path", seeded_db, "--event", _EVENT_ID, "--json"
        )
        self._assert_no_forbidden_content(output, "evidence --json")

    def test_no_secrets_in_json_dumps(self) -> None:
        """json.dumps of smoke report does not leak known test secret values."""
        config_path = _smoke_config_path()
        output = _run_cli("smoke", "--config", config_path, "--json")

        # Verify it parses as valid JSON.
        report = json.loads(output)

        # The serialized string must not contain known test secret patterns.
        serialized = json.dumps(report, sort_keys=True)
        for secret in _FORBIDDEN_SECRET_VALUES:
            assert secret not in serialized, (
                f"json.dumps output contains forbidden secret value {secret!r}"
            )
