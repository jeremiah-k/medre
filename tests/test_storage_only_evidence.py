"""Storage-only evidence bundle tests.

Proves that :func:`medre.runtime.evidence.collect_evidence_bundle` in
``storage_path`` mode is fully network-free, requires no config, no adapter
SDKs, and no live health dependency.  Every test uses only a local SQLite
file.

Covers:

- Storage-only mode with ``event_id`` returns event summary, native refs,
  delivery receipts.
- Storage-only mode without ``event_id`` returns counts only.
- Missing ``event_id`` produces partial status (not traceback).
- Malformed / missing SQLite DB produces error/partial status (not traceback).
- No adapter SDK imports in the storage-only code path.
- No secret leakage in storage-only output.
- Native refs field shape matches agreement requirements.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.runtime.evidence._bundle import collect_evidence_bundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_db_with_event_and_receipts(
    db_path: str,
) -> tuple[str, str]:
    """Create a populated DB with one event, one receipt, one native ref.

    Returns ``(event_id, receipt_id)``.
    """
    from medre.core.events.canonical import (
        CanonicalEvent,
        DeliveryReceipt,
        NativeMessageRef,
    )
    from medre.core.events.kinds import EventKind
    from medre.core.events.metadata import EventMetadata
    from medre.core.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(db_path)
    await storage.initialize()

    event = CanonicalEvent(
        event_id="ev-storage-only-001",
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter="main",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "storage-only evidence test"},
        metadata=EventMetadata(),
    )
    await storage.append(event)

    receipt = DeliveryReceipt(
        receipt_id="rcpt-so-001",
        event_id="ev-storage-only-001",
        delivery_plan_id="dp-so-001",
        target_adapter="radio",
        status="sent",
        source="live",
        created_at=datetime(2026, 3, 15, 12, 0, 1, tzinfo=timezone.utc),
    )
    await storage.append_receipt(receipt)

    native_ref = NativeMessageRef(
        id="nref-so-001",
        event_id="ev-storage-only-001",
        adapter="radio",
        native_channel_id="!room:target",
        native_message_id="$native-msg-001",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        metadata={},
        created_at=datetime(2026, 3, 15, 12, 0, 1, tzinfo=timezone.utc),
    )
    await storage.store_native_ref(native_ref)

    await storage.close()
    return event.event_id, receipt.receipt_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStorageOnlyWithEventId:
    """Storage-only mode with event_id returns full event details."""

    @pytest.mark.asyncio
    async def test_returns_event_summary(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        storage = report["sections"]["storage"]
        assert storage["status"] == "passed"
        assert storage["data"]["event"] is not None
        assert storage["data"]["event"]["event_id"] == event_id

    @pytest.mark.asyncio
    async def test_returns_native_refs(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        native_refs = report["sections"]["storage"]["data"]["native_refs_for_event"]
        assert native_refs is not None
        assert len(native_refs) == 1
        assert native_refs[0]["adapter"] == "radio"
        assert native_refs[0]["native_message_id"] == "$native-msg-001"

    @pytest.mark.asyncio
    async def test_returns_delivery_receipts(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        data = report["sections"]["storage"]["data"]
        # Timeline includes both event and receipt entries.
        assert data["timeline"] is not None
        assert len(data["timeline"]) > 0

    @pytest.mark.asyncio
    async def test_incident_summary_present(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary is not None
        assert summary["event_id"] == event_id
        assert summary["classification"] == "success"
        assert summary["sent_count"] == 1
        assert summary["failed_count"] == 0

    @pytest.mark.asyncio
    async def test_config_sections_skipped(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        assert report["config_source"] == "storage_path"
        assert report["runtime_started"] is False
        assert report["sections"]["config_summary"]["status"] == "skipped"
        assert report["sections"]["route_validation"]["status"] == "skipped"
        assert report["sections"]["diagnostics_snapshot"]["status"] == "skipped"
        assert report["sections"]["live_health"]["status"] == "skipped"


class TestStorageOnlyWithoutEventId:
    """Storage-only mode without event_id returns counts only."""

    @pytest.mark.asyncio
    async def test_returns_event_count(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(storage_path=db_path)
        data = report["sections"]["storage"]["data"]
        assert data["event_count"] == 1

    @pytest.mark.asyncio
    async def test_returns_receipt_count(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(storage_path=db_path)
        data = report["sections"]["storage"]["data"]
        assert data["receipt_count"] == 1

    @pytest.mark.asyncio
    async def test_no_event_data_without_event_id(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(storage_path=db_path)
        data = report["sections"]["storage"]["data"]
        assert data["event"] is None
        assert data.get("incident_summary") is None


class TestStorageOnlyMissingEventId:
    """Storage-only mode with a non-existent event_id returns partial."""

    @pytest.mark.asyncio
    async def test_partial_not_found(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-nonexistent-999",
        )
        storage = report["sections"]["storage"]
        assert storage["status"] == "partial"
        assert storage["data"]["event"] is None
        assert storage["error"] is not None
        assert "not found" in storage["error"].lower()

    @pytest.mark.asyncio
    async def test_counts_still_present(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-nonexistent-999",
        )
        data = report["sections"]["storage"]["data"]
        # Even when event not found, counts are populated.
        assert data["event_count"] == 1
        assert data["receipt_count"] == 1

    @pytest.mark.asyncio
    async def test_no_traceback(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-nonexistent-999",
        )
        raw = json.dumps(report)
        assert "Traceback" not in raw


class TestStorageOnlyMissingOrMalformedDb:
    """Storage-only mode with missing/malformed DB produces graceful status."""

    @pytest.mark.asyncio
    async def test_missing_db_partial(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "does_not_exist.db")

        report = await collect_evidence_bundle(storage_path=db_path)
        storage = report["sections"]["storage"]
        assert storage["status"] == "partial"
        assert storage["data"]["db_exists"] is False
        assert storage["error"] is not None

    @pytest.mark.asyncio
    async def test_missing_db_no_traceback(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "does_not_exist.db")

        report = await collect_evidence_bundle(storage_path=db_path)
        raw = json.dumps(report)
        assert "Traceback" not in raw

    @pytest.mark.asyncio
    async def test_missing_db_no_file_created(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "does_not_exist.db")

        await collect_evidence_bundle(storage_path=db_path)
        assert not Path(db_path).exists()

    @pytest.mark.asyncio
    async def test_malformed_db_partial(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "malformed.db")
        Path(db_path).write_text("this is not a sqlite database file")

        report = await collect_evidence_bundle(storage_path=db_path)
        storage = report["sections"]["storage"]
        assert storage["status"] in ("partial", "error")
        assert storage["error"] is not None

    @pytest.mark.asyncio
    async def test_malformed_db_no_traceback(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "malformed.db")
        Path(db_path).write_text("this is not a sqlite database file")

        report = await collect_evidence_bundle(storage_path=db_path)
        raw = json.dumps(report)
        assert "Traceback" not in raw

    @pytest.mark.asyncio
    async def test_malformed_db_error_in_errors_list(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "malformed.db")
        Path(db_path).write_text("garbage data")

        report = await collect_evidence_bundle(storage_path=db_path)
        assert len(report["errors"]) > 0


class TestStorageOnlyEmptyDb:
    """Storage-only mode with a valid but empty SQLite DB."""

    @pytest.mark.asyncio
    async def test_empty_db_returns_zero_counts(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "empty.db")
        from medre.core.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)
        section = report["sections"]["storage"]
        assert section["status"] == "passed"
        assert section["data"]["db_exists"] is True
        assert section["data"]["event_count"] == 0
        assert section["data"]["receipt_count"] == 0


class TestStorageOnlyNoAdapterSdkImports:
    """Storage-only code path does not import any adapter SDKs."""

    def test_storage_sections_no_adapter_imports(self) -> None:
        import inspect

        from medre.runtime.evidence import _storage_sections

        source = inspect.getsource(_storage_sections)
        adapter_sdk_patterns = [
            "import nio",
            "import meshtastic",
            "import meshcore",
            "import lxmf",
            "from nio",
            "from meshtastic",
            "from meshcore",
            "from lxmf",
            "from medre.adapters.matrix.adapter",
            "from medre.adapters.meshtastic",
            "from medre.adapters.meshcore",
            "from medre.adapters.lxmf",
        ]
        for pattern in adapter_sdk_patterns:
            assert pattern not in source, (
                f"Storage-only module should not import adapter SDKs, "
                f"found: {pattern!r}"
            )

    def test_bundle_module_no_adapter_imports(self) -> None:
        import inspect

        from medre.runtime.evidence import _bundle

        source = inspect.getsource(_bundle)
        adapter_sdk_patterns = [
            "import nio",
            "import meshtastic",
            "import meshcore",
            "import lxmf",
            "from nio",
            "from meshtastic",
            "from meshcore",
            "from lxmf",
            "from medre.adapters.matrix.adapter",
            "from medre.adapters.meshtastic",
            "from medre.adapters.meshcore",
            "from medre.adapters.lxmf",
        ]
        for pattern in adapter_sdk_patterns:
            assert pattern not in source, (
                f"Bundle module should not import adapter SDKs, " f"found: {pattern!r}"
            )


class TestStorageOnlyNoSecretLeakage:
    """Storage-only output never contains secret values."""

    SECRET_PATTERNS = (
        "syt_",
        "access_token",
        "password",
        "api_key",
        "secret",
    )

    @pytest.mark.asyncio
    async def test_no_secrets_in_output(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        raw = json.dumps(report, sort_keys=True).lower()
        # None of these secret-key names should appear as keys or values.
        assert '"access_token"' not in raw
        assert '"password"' not in raw
        assert '"api_key"' not in raw

    @pytest.mark.asyncio
    async def test_error_messages_sanitized(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "malformed.db")
        Path(db_path).write_text("not a database with syt_secret_token_xyz")

        report = await collect_evidence_bundle(storage_path=db_path)
        raw = json.dumps(report)
        assert "syt_secret_token_xyz" not in raw

    @pytest.mark.asyncio
    async def test_report_is_json_safe(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(storage_path=db_path)
        # Must not raise — proves everything is JSON-serialisable.
        raw = json.dumps(report, sort_keys=True, indent=2)
        parsed = json.loads(raw)
        assert parsed["schema_version"] == 1


class TestNativeRefsFieldShape:
    """Native refs field shape matches agreement requirements."""

    REQUIRED_FIELDS = (
        "adapter",
        "native_channel_id",
        "native_message_id",
        "direction",
    )

    ALL_FIELDS = (
        "id",
        "event_id",
        "adapter",
        "native_channel_id",
        "native_message_id",
        "native_thread_id",
        "native_relation_id",
        "direction",
        "created_at",
    )

    @pytest.mark.asyncio
    async def test_required_fields_present(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        native_refs = report["sections"]["storage"]["data"]["native_refs_for_event"]
        assert len(native_refs) == 1
        ref = native_refs[0]
        for field in self.REQUIRED_FIELDS:
            assert field in ref, f"Missing required field: {field}"

    @pytest.mark.asyncio
    async def test_field_values_correct(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        ref = report["sections"]["storage"]["data"]["native_refs_for_event"][0]
        assert ref["adapter"] == "radio"
        assert ref["native_channel_id"] == "!room:target"
        assert ref["native_message_id"] == "$native-msg-001"
        assert ref["direction"] == "outbound"

    @pytest.mark.asyncio
    async def test_all_expected_fields_present(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        ref = report["sections"]["storage"]["data"]["native_refs_for_event"][0]
        for field in self.ALL_FIELDS:
            assert field in ref, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_no_extra_secret_fields(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        event_id, _ = await _make_db_with_event_and_receipts(db_path)

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event_id,
        )
        ref = report["sections"]["storage"]["data"]["native_refs_for_event"][0]
        forbidden = ("access_token", "password", "secret", "api_key", "credentials")
        for key in ref:
            assert key not in forbidden, f"Forbidden field in native ref: {key}"
