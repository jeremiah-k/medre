"""TDD tests for Tranche 2 target-keyed delivery evidence.

Defines the desired ``delivery_state_by_target`` shape and verifies the
target-keyed composite key grouping replaces the old adapter-keyed
``delivery_state_by_adapter`` collapse.

Every test:

- Uses **SQLite storage** — no live transports or SDKs.
- Calls ``collect_evidence_bundle(storage_path=...)`` directly.
- Asserts the **new** ``delivery_state_by_target`` shape only.
- Expects ``delivery_state_by_adapter`` to be **absent**.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.storage.sqlite import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(
    year: int = 2026,
    month: int = 1,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _make_event(
    event_id: str = "ev-target-keyed-001",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src-adapter",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "target-keyed test"},
        metadata=EventMetadata(),
    )


def _receipt(
    *,
    receipt_id: str = "rcpt-001",
    event_id: str = "ev-target-keyed-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    delivery_plan_id: str = "dp-001",
    status: str = "sent",
    attempt_number: int = 1,
    error: str | None = None,
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
    source: str = "live",
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,
        error=error,
        failure_kind=failure_kind,
        attempt_number=attempt_number,
        next_retry_at=next_retry_at,
        source=source,
        created_at=_ts(second=1),
    )


async def _build_db(
    db_path: str,
    event_id: str,
    receipts: list[DeliveryReceipt],
) -> None:
    """Create a SQLite DB with one event and arbitrary receipts."""
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    event = _make_event(event_id=event_id)
    await storage.append(event)
    for r in receipts:
        await storage.append_receipt(r)
    await storage.close()


async def _get_incident_summary(
    db_path: str,
    event_id: str,
) -> dict[str, Any]:
    """Collect evidence bundle and return incident_summary dict."""
    report = await collect_evidence_bundle(
        storage_path=db_path,
        event_id=event_id,
    )
    return report["sections"]["storage"]["data"]["incident_summary"]


# Required decomposed fields in each delivery_state_by_target value.
_DECOMPOSED_FIELDS = (
    "target_adapter",
    "target_channel",
    "route_id",
    "delivery_plan_id",
    "status",
    "attempt_number",
    "failure_kind",
    "failure_kind_detail",
    "retryable",
    "next_retry_at",
)


# ===================================================================
# 1. Single adapter / single channel → one entry
# ===================================================================


class TestSingleAdapterSingleChannel:
    """One adapter, one channel produces exactly one ``delivery_state_by_target`` entry."""

    @pytest.mark.asyncio
    async def test_one_entry(self, tmp_path: Any) -> None:
        event_id = "ev-tk-single-001"
        db_path = str(tmp_path / "single.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-s-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)

        dsbt = summary["delivery_state_by_target"]
        assert isinstance(dsbt, dict), (
            f"delivery_state_by_target must be dict, "
            f"got {type(dsbt).__name__}"
        )
        assert len(dsbt) == 1, (
            f"Expected exactly 1 entry for single adapter/single channel, "
            f"got {len(dsbt)}: {list(dsbt.keys())}"
        )

    @pytest.mark.asyncio
    async def test_entry_has_all_decomposed_fields(self, tmp_path: Any) -> None:
        event_id = "ev-tk-fields-001"
        db_path = str(tmp_path / "fields.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-f-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        assert len(dsbt) == 1

        entry = next(iter(dsbt.values()))
        for field in _DECOMPOSED_FIELDS:
            assert field in entry, (
                f"delivery_state_by_target entry missing '{field}'. "
                f"Available keys: {sorted(entry.keys())}"
            )

    @pytest.mark.asyncio
    async def test_entry_values_match_receipt(self, tmp_path: Any) -> None:
        event_id = "ev-tk-values-001"
        db_path = str(tmp_path / "values.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-v-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))

        assert entry["target_adapter"] == "radio"
        assert entry["target_channel"] == "ch-0"
        assert entry["route_id"] == "route-a"
        assert entry["delivery_plan_id"] == "dp-001"
        assert entry["status"] == "sent"
        assert entry["attempt_number"] == 1
        assert entry["failure_kind"] is None
        assert entry["retryable"] is False


# ===================================================================
# 2. Same adapter, two channels (one sent, one failed) → two entries
# ===================================================================


class TestSameAdapterTwoChannels:
    """Same adapter with two channels: both visible, failed channel NOT hidden."""

    @pytest.mark.asyncio
    async def test_two_entries_not_collapsed(self, tmp_path: Any) -> None:
        event_id = "ev-tk-twoch-001"
        db_path = str(tmp_path / "twoch.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-tc-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-sent",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
                _receipt(
                    receipt_id="rcpt-tc-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-failed",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError: connection timed out",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 2, (
            f"Same adapter with 2 channels must produce 2 entries, "
            f"got {len(dsbt)}: {list(dsbt.keys())}"
        )

    @pytest.mark.asyncio
    async def test_failed_channel_not_hidden(self, tmp_path: Any) -> None:
        event_id = "ev-tk-failvis-001"
        db_path = str(tmp_path / "failvis.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-fv-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-sent",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
                _receipt(
                    receipt_id="rcpt-fv-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-failed",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError: connection timed out",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        # Find the failed entry
        failed_entries = [
            (k, v) for k, v in dsbt.items() if v.get("status") == "failed"
        ]
        assert len(failed_entries) == 1, (
            f"Expected exactly 1 failed entry, got {len(failed_entries)}. "
            f"All entries: {[(k, v.get('status')) for k, v in dsbt.items()]}"
        )
        _, failed = failed_entries[0]
        assert failed["target_channel"] == "ch-failed"
        assert failed["target_adapter"] == "radio"
        assert failed["failure_kind"] == "adapter_transient"

        # Find the sent entry
        sent_entries = [
            (k, v) for k, v in dsbt.items() if v.get("status") == "sent"
        ]
        assert len(sent_entries) == 1
        _, sent = sent_entries[0]
        assert sent["target_channel"] == "ch-sent"


# ===================================================================
# 3. Same adapter, same channel, different route_ids → two entries
# ===================================================================


class TestSameAdapterSameChannelDifferentRoutes:
    """Same adapter and channel but different route IDs produce distinct entries."""

    @pytest.mark.asyncio
    async def test_two_entries_by_route_id(self, tmp_path: Any) -> None:
        event_id = "ev-tk-routes-001"
        db_path = str(tmp_path / "routes.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-r-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-alpha",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
                _receipt(
                    receipt_id="rcpt-r-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-beta",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 2, (
            f"Same adapter/channel but different route_ids must produce 2 entries, "
            f"got {len(dsbt)}: {list(dsbt.keys())}"
        )

    @pytest.mark.asyncio
    async def test_route_ids_preserved_in_entries(self, tmp_path: Any) -> None:
        event_id = "ev-tk-rids-001"
        db_path = str(tmp_path / "rids.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-rid-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-alpha",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
                _receipt(
                    receipt_id="rcpt-rid-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-beta",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        route_ids_in_entries = {v["route_id"] for v in dsbt.values()}
        assert route_ids_in_entries == {"route-alpha", "route-beta"}, (
            f"Expected route_alpha and route_beta in entries, "
            f"got {route_ids_in_entries}"
        )


# ===================================================================
# 4. Retry / multi-attempt → highest attempt per target key
# ===================================================================


class TestRetryMultiAttempt:
    """Multi-attempt receipts select highest attempt_number per target key."""

    @pytest.mark.asyncio
    async def test_highest_attempt_selected(self, tmp_path: Any) -> None:
        event_id = "ev-tk-retry-001"
        db_path = str(tmp_path / "retry.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-att-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    attempt_number=1,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="rcpt-att-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    attempt_number=2,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="rcpt-att-3",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    attempt_number=3,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        # Only one entry for this target key
        assert len(dsbt) == 1, (
            f"Expected 1 entry for same target key with 3 attempts, "
            f"got {len(dsbt)}"
        )
        entry = next(iter(dsbt.values()))
        assert entry["attempt_number"] == 3, (
            f"Expected attempt_number == 3 (highest), "
            f"got {entry['attempt_number']}"
        )

    @pytest.mark.asyncio
    async def test_highest_attempt_reflects_latest_status(
        self, tmp_path: Any
    ) -> None:
        """When attempt 2 succeeded but attempt 3 exists, entry shows attempt 3."""
        event_id = "ev-tk-retry-status-001"
        db_path = str(tmp_path / "retry-status.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-rs-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                    attempt_number=1,
                ),
                _receipt(
                    receipt_id="rcpt-rs-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                    attempt_number=2,
                ),
                _receipt(
                    receipt_id="rcpt-rs-3",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    attempt_number=3,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))
        assert entry["attempt_number"] == 3
        assert entry["status"] == "failed"


# ===================================================================
# 5. dead_lettered + sent on different channels → both visible
# ===================================================================


class TestDeadLetteredAndSentDifferentChannels:
    """dead_lettered on one channel and sent on another: both entries visible."""

    @pytest.mark.asyncio
    async def test_both_channels_visible(self, tmp_path: Any) -> None:
        event_id = "ev-tk-dl-sent-001"
        db_path = str(tmp_path / "dl-sent.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-dl-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-dl",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                    attempt_number=4,
                ),
                _receipt(
                    receipt_id="rcpt-dl-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-ok",
                    route_id="route-b",
                    delivery_plan_id="dp-002",
                    status="sent",
                    attempt_number=1,
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 2, (
            f"dead_lettered + sent on different channels must produce 2 entries, "
            f"got {len(dsbt)}: {list(dsbt.keys())}"
        )

        statuses = {v["status"] for v in dsbt.values()}
        assert statuses == {"dead_lettered", "sent"}, (
            f"Expected both 'dead_lettered' and 'sent' statuses, got {statuses}"
        )

    @pytest.mark.asyncio
    async def test_dead_lettered_entry_fields(self, tmp_path: Any) -> None:
        event_id = "ev-tk-dl-fields-001"
        db_path = str(tmp_path / "dl-fields.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-dlf-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-dl",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                    attempt_number=4,
                ),
                _receipt(
                    receipt_id="rcpt-dlf-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-ok",
                    route_id="route-b",
                    delivery_plan_id="dp-002",
                    status="sent",
                    attempt_number=1,
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        dl_entries = [
            v for v in dsbt.values() if v.get("status") == "dead_lettered"
        ]
        assert len(dl_entries) == 1
        dl = dl_entries[0]
        assert dl["target_channel"] == "ch-dl"
        assert dl["target_adapter"] == "radio"
        assert dl["attempt_number"] == 4
        assert dl["failure_kind"] == "adapter_transient"
        assert dl["retryable"] is False


# ===================================================================
# 6. Composite key format is deterministic and JSON-safe
# ===================================================================


class TestCompositeKeyDeterministicJsonSafe:
    """Composite keys in delivery_state_by_target are deterministic and JSON-safe."""

    @pytest.mark.asyncio
    async def test_key_is_json_parseable(self, tmp_path: Any) -> None:
        """Each key in delivery_state_by_target can be parsed as JSON."""
        event_id = "ev-tk-jsonkey-001"
        db_path = str(tmp_path / "jsonkey.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-jk-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        for key in dsbt:
            parsed = json.loads(key)
            assert isinstance(parsed, dict), (
                f"Composite key must be a JSON dict, got {type(parsed).__name__}: {key!r}"
            )

    @pytest.mark.asyncio
    async def test_key_contains_target_components(self, tmp_path: Any) -> None:
        """Parsed key contains target_adapter, target_channel, route_id, delivery_plan_id."""
        event_id = "ev-tk-keycomp-001"
        db_path = str(tmp_path / "keycomp.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-kc-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-42",
                    route_id="route-x",
                    delivery_plan_id="dp-007",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 1
        key = next(iter(dsbt.keys()))
        parsed = json.loads(key)
        assert parsed["target_adapter"] == "radio"
        assert parsed["target_channel"] == "ch-42"
        assert parsed["route_id"] == "route-x"
        assert parsed["delivery_plan_id"] == "dp-007"

    @pytest.mark.asyncio
    async def test_key_deterministic_for_same_inputs(self, tmp_path: Any) -> None:
        """Same target components always produce the same key."""
        event_id = "ev-tk-detkey-001"
        db_path = str(tmp_path / "detkey.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-dk-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        key = next(iter(dsbt.keys()))
        # Re-serialise parsed key and verify round-trip stability.
        parsed = json.loads(key)
        re_serialised = json.dumps(parsed, sort_keys=True)
        re_parsed = json.loads(re_serialised)
        assert parsed == re_parsed, (
            f"Composite key not stable through JSON round-trip: "
            f"{parsed!r} != {re_parsed!r}"
        )

    @pytest.mark.asyncio
    async def test_full_summary_json_roundtrip(self, tmp_path: Any) -> None:
        """delivery_state_by_target survives full JSON serialisation."""
        event_id = "ev-tk-roundtrip-001"
        db_path = str(tmp_path / "roundtrip.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-rt-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
                _receipt(
                    receipt_id="rcpt-rt-2",
                    event_id=event_id,
                    target_adapter="matrix",
                    target_channel="!room:test",
                    route_id="route-b",
                    delivery_plan_id="dp-002",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)

        # Must be JSON-serialisable without error.
        raw = json.dumps(summary, sort_keys=True)
        reloaded = json.loads(raw)

        dsbt = reloaded["delivery_state_by_target"]
        assert isinstance(dsbt, dict)
        assert len(dsbt) == 2

        # Every key must parse cleanly.
        for key in dsbt:
            json.loads(key)


# ===================================================================
# 7. Each value includes all decomposed fields
# ===================================================================


class TestDecomposedFieldCoverage:
    """Every delivery_state_by_target value includes all required decomposed fields."""

    @pytest.mark.asyncio
    async def test_sent_entry_all_fields(self, tmp_path: Any) -> None:
        event_id = "ev-tk-decomp-sent-001"
        db_path = str(tmp_path / "decomp-sent.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-ds-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))

        for field in _DECOMPOSED_FIELDS:
            assert field in entry, (
                f"Sent entry missing field '{field}'. "
                f"Available: {sorted(entry.keys())}"
            )

        # Verify field values
        assert entry["target_adapter"] == "radio"
        assert entry["target_channel"] == "ch-0"
        assert entry["route_id"] == "route-a"
        assert entry["delivery_plan_id"] == "dp-001"
        assert entry["status"] == "sent"
        assert entry["attempt_number"] == 1
        assert entry["failure_kind"] is None
        assert entry["retryable"] is False
        assert entry["next_retry_at"] is None

    @pytest.mark.asyncio
    async def test_failed_entry_all_fields(self, tmp_path: Any) -> None:
        event_id = "ev-tk-decomp-fail-001"
        db_path = str(tmp_path / "decomp-fail.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-df-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError: connection timed out",
                    next_retry_at=_ts(hour=0, minute=0, second=30),
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))

        for field in _DECOMPOSED_FIELDS:
            assert field in entry, (
                f"Failed entry missing field '{field}'. "
                f"Available: {sorted(entry.keys())}"
            )

        assert entry["target_adapter"] == "radio"
        assert entry["status"] == "failed"
        assert entry["failure_kind"] == "adapter_transient"
        assert entry["failure_kind_detail"] == "adapter_transient"
        assert entry["retryable"] is True
        assert entry["next_retry_at"] is not None

    @pytest.mark.asyncio
    async def test_suppressed_entry_all_fields(self, tmp_path: Any) -> None:
        event_id = "ev-tk-decomp-supp-001"
        db_path = str(tmp_path / "decomp-supp.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-dsupp-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="Self-loop guard",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))

        for field in _DECOMPOSED_FIELDS:
            assert field in entry, (
                f"Suppressed entry missing field '{field}'. "
                f"Available: {sorted(entry.keys())}"
            )

        assert entry["status"] == "suppressed"
        assert entry["failure_kind"] == "loop_suppressed"
        assert entry["retryable"] is False

    @pytest.mark.asyncio
    async def test_dead_lettered_entry_all_fields(self, tmp_path: Any) -> None:
        event_id = "ev-tk-decomp-dl-001"
        db_path = str(tmp_path / "decomp-dl.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-ddl-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                    attempt_number=5,
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]

        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))

        for field in _DECOMPOSED_FIELDS:
            assert field in entry, (
                f"Dead-lettered entry missing field '{field}'. "
                f"Available: {sorted(entry.keys())}"
            )

        assert entry["status"] == "dead_lettered"
        assert entry["attempt_number"] == 5
        assert entry["retryable"] is False


# ===================================================================
# 8. delivery_state_by_adapter is ABSENT
# ===================================================================


class TestDeliveryStateByAdapterAbsent:
    """The old delivery_state_by_adapter must not appear in incident_summary."""

    @pytest.mark.asyncio
    async def test_adapter_key_absent(self, tmp_path: Any) -> None:
        event_id = "ev-tk-no-adapter-001"
        db_path = str(tmp_path / "no-adapter.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-na-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)

        assert "delivery_state_by_adapter" not in summary, (
            "delivery_state_by_adapter must be removed from incident_summary "
            "and replaced with delivery_state_by_target"
        )

    @pytest.mark.asyncio
    async def test_adapter_key_absent_multi_receipt(self, tmp_path: Any) -> None:
        """Even with multiple receipts, delivery_state_by_adapter must not appear."""
        event_id = "ev-tk-no-adapter-multi-001"
        db_path = str(tmp_path / "no-adapter-multi.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-nam-1",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-a",
                    delivery_plan_id="dp-001",
                    status="sent",
                ),
                _receipt(
                    receipt_id="rcpt-nam-2",
                    event_id=event_id,
                    target_adapter="radio",
                    target_channel="ch-1",
                    route_id="route-b",
                    delivery_plan_id="dp-002",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)

        assert "delivery_state_by_adapter" not in summary, (
            "delivery_state_by_adapter must be removed from incident_summary "
            "even with multiple receipts"
        )
