"""Focused tests for delivery evidence unification across the pipeline.

Covers the operator-facing explanation/reporting contract without running
live adapters or requiring external services.  Tests exercise:

- Success explanation/report dict includes native/adaptor message ID.
- Transient failure explanation/report includes retryable=true or
  adapter_transient classification.
- Permanent failure includes retryable=false or adapter_permanent.
- Dead-letter incident summary includes attempts/exhaustion evidence
  (dead_lettered_count, retry fields).
- Loop suppressed visibility: DeliveryOutcome may be skipped; pipeline persists
  suppressed receipts (status="suppressed") for loop/capacity/shutdown where
  event/target context exists.
- duplicate_suppressed: removed from enum (was never emitted).
- Matrix success metadata includes matrix_txn_id.
- Matrix E2EE blocked is permanent/recognizable.
- Meshtastic queue full/rejected is transient/queue rejected.
- Meshtastic classifier ignored/drop/deferred aggregate diagnostics
  counters are present.
- JSON/evidence output is stable and secret-safe.

Uses in-repo fake adapters and mocks only.  No pytest execution required.
py_compile validation only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.rendering.renderer import RenderingResult
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.reporting import delivery_receipt_to_report_dict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_receipt(
    *,
    status: str = "sent",
    adapter_message_id: str | None = None,
    failure_kind: str | None = None,
    attempt_number: int = 1,
    error: str | None = None,
    retry_max_attempts: int | None = None,
    retry_backoff_base: float | None = None,
) -> DeliveryReceipt:
    from typing import cast

    from medre.core.events.canonical import DeliveryReceipt

    valid_statuses = (
        "queued",
        "sent",
        "failed",
        "dead_lettered",
        "suppressed",
    )
    assert status in valid_statuses, f"Invalid receipt status: {status!r}"
    return DeliveryReceipt(
        receipt_id=f"rcpt-unif-{attempt_number}",
        event_id="evt-unif-001",
        delivery_plan_id="plan-unif-001",
        target_adapter="target_adapter",
        target_channel="ch-0",
        route_id="route-unif",
        status=cast(Any, status),
        adapter_message_id=adapter_message_id,
        failure_kind=failure_kind,
        attempt_number=attempt_number,
        error=error,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_base=retry_backoff_base,
        created_at=datetime.now(timezone.utc),
    )


def _make_outcome(
    *,
    status: str = "success",
    failure_kind: DeliveryFailureKind | None = None,
    receipt: DeliveryReceipt | None = None,
    error: str | None = None,
) -> DeliveryOutcome:
    from typing import cast

    valid_statuses = (
        "success",
        "queued",
        "transient_failure",
        "permanent_failure",
        "skipped",
    )
    assert status in valid_statuses, f"Invalid outcome status: {status!r}"
    return DeliveryOutcome(
        event_id="evt-unif-001",
        target_adapter="target_adapter",
        target_channel="ch-0",
        route_id="route-unif",
        delivery_plan_id="plan-unif-001",
        status=cast(Any, status),
        failure_kind=failure_kind,
        receipt=receipt,
        error=error,
    )


def _matrix_result(
    event_id: str = "evt-matrix-unif",
    target_channel: str = "!room:example.com",
    body: str = "hello",
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter="matrix-unif",
        target_channel=target_channel,
        payload={"msgtype": "m.text", "body": body},
    )


def _matrix_config(**overrides: Any) -> Any:
    from medre.config.adapters.matrix import MatrixConfig

    defaults: dict[str, Any] = {
        "adapter_id": "matrix-unif",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_secret_do_not_expose",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _adapter_context(adapter_id: str = "matrix-unif") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _mock_send_response(event_id: str = "$sent-unif-001") -> MagicMock:
    resp = MagicMock()
    resp.event_id = event_id
    return resp


# ---------------------------------------------------------------------------
# Sample TOML config & fixture (moved from test_evidence_cli.py)
# ---------------------------------------------------------------------------

CONFIG_FAKE_ADAPTERS = """\
[runtime]
name = "test-evidence"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test_evidence.db"

[adapters.matrix.main]
enabled = true
adapter_kind = "fake"
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "syt_super_secret_token_12345"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
adapter_kind = "fake"
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "TestMesh"

[routes.bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
"""


@pytest.fixture()
def config_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write fake-adapter config to temp file with MEDRE_HOME isolation."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_FAKE_ADAPTERS)
    return p


async def _make_populated_db_with_suppressed(
    db_path: str,
    event_id: str = "ev-suppressed-001",
    failure_kind: str = "loop_suppressed",
    error: str | None = "Loop suppressed: event already delivered to target",
) -> str:
    """Create a DB with a suppressed receipt carrying a persisted failure_kind.

    Returns the event_id.
    """
    from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
    from medre.core.events.kinds import EventKind
    from medre.core.events.metadata import EventMetadata
    from medre.core.storage import SQLiteStorage

    storage = SQLiteStorage(db_path)
    await storage.initialize()

    event = CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        source_adapter="main",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "suppressed evidence test"},
        metadata=EventMetadata(),
    )
    await storage.append(event)

    receipt = DeliveryReceipt(
        receipt_id="rcpt-supp-001",
        event_id=event_id,
        delivery_plan_id="dp-supp-001",
        target_adapter="radio",
        status="suppressed",
        source="live",
        failure_kind=failure_kind,
        error=error,
        created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    await storage.append_receipt(receipt)

    await storage.close()
    return event_id


# ===================================================================
# 0a. _compute_retryable through delivery_receipt_to_report_dict
# ===================================================================


class TestComputeRetryable:
    """Retryable flag derivation from receipt fields via public report dict.

    Rules under test:
    * ``dead_lettered`` + ``adapter_transient`` → retryable ``False``.
    * ``suppressed`` + ``adapter_transient`` → retryable ``False``.
    * ``failed`` + ``adapter_transient`` + no ``next_retry_at`` → ``True``.
    * ``failed`` + ``adapter_permanent`` → ``False``.
    * Any receipt with ``next_retry_at`` → ``True`` unless status is
      ``dead_lettered`` or ``suppressed``.
    """

    def test_dead_lettered_adapter_transient_not_retryable(self) -> None:
        """Dead-lettered receipt with adapter_transient is not retryable."""
        receipt = _make_receipt(
            status="dead_lettered",
            failure_kind="adapter_transient",
            error="Retry exhausted after 3 attempts",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is False

    def test_suppressed_adapter_transient_not_retryable(self) -> None:
        """Suppressed receipt with adapter_transient is not retryable."""
        receipt = _make_receipt(
            status="suppressed",
            failure_kind="adapter_transient",
            error="Suppressed by loop guard",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is False

    def test_failed_adapter_transient_no_retry_at_is_retryable(self) -> None:
        """Failed receipt with adapter_transient and no next_retry_at is retryable."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is True

    def test_failed_adapter_permanent_not_retryable(self) -> None:
        """Failed receipt with adapter_permanent is not retryable."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_permanent",
            error="ValueError: malformed payload",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is False

    def test_next_retry_at_makes_failed_retryable(self) -> None:
        """Failed receipt with next_retry_at scheduled is retryable."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
        )
        # Override next_retry_at — _make_receipt doesn't expose it directly.
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter=receipt.target_adapter,
            target_channel=receipt.target_channel,
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
            attempt_number=2,
            next_retry_at=datetime.now(timezone.utc),
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is True

    def test_dead_lettered_with_next_retry_at_not_retryable(self) -> None:
        """Dead-lettered receipt with next_retry_at is still not retryable."""
        receipt = DeliveryReceipt(
            receipt_id="rcpt-dl-retry",
            event_id="evt-dl-retry",
            delivery_plan_id="plan-dl-retry",
            target_adapter="adapter-dl",
            target_channel="ch-0",
            route_id="route-dl",
            status="dead_lettered",
            failure_kind="adapter_transient",
            error="Retry exhausted after 3 attempts",
            attempt_number=4,
            next_retry_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is False

    def test_suppressed_with_next_retry_at_not_retryable(self) -> None:
        """Suppressed receipt with next_retry_at is still not retryable."""
        receipt = DeliveryReceipt(
            receipt_id="rcpt-supp-retry",
            event_id="evt-supp-retry",
            delivery_plan_id="plan-supp-retry",
            target_adapter="adapter-supp",
            target_channel="ch-0",
            route_id="route-supp",
            status="suppressed",
            failure_kind="loop_suppressed",
            error="Loop prevented delivery",
            attempt_number=1,
            next_retry_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is False


# ===================================================================
# 0b. _derive_failure_kind_detail through delivery_receipt_to_report_dict
# ===================================================================


class TestDeriveFailureKindDetail:
    """Failure-kind detail derivation from error context via public report dict.

    Patterns under test:
    * Meshtastic queue-full / enqueue-rejected error with adapter name
      containing "meshtastic" → ``"meshtastic_queue_rejected"``.
    * Meshtastic queue error with adapter name "radio" (config alias) →
      ``"meshtastic_queue_rejected"`` (error text is authoritative).
    * Meshtastic queue error with no "meshtastic" in error text
      (e.g. "queue is full; enqueue rejected") → still detected.
    * Unrelated error where neither ``queue+full`` nor ``enqueue rejected``
      are present → original ``failure_kind``.
    * No ``failure_kind`` at all → ``None``.
    * E2EE: Matrix-specific patterns (e2ee, megolm, olm session, etc.)
      → ``"e2ee_blocked"``.
    * E2EE: generic "encrypted packet" does NOT match → original
      ``failure_kind``.
    """

    def test_meshtastic_adapter_queue_full_rejected(self) -> None:
        """target_adapter='meshtastic' with queue-full error produces
        failure_kind_detail='meshtastic_queue_rejected'."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="Meshtastic outbound queue is full; enqueue rejected (1/1)",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="meshtastic",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="Meshtastic outbound queue is full; enqueue rejected (1/1)",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "meshtastic_queue_rejected"

    def test_radio_adapter_meshtastic_queue_error_detail(self) -> None:
        """target_adapter='radio' (config alias) with Meshtastic queue-full
        error text produces failure_kind_detail='meshtastic_queue_rejected'.

        The error text is the authoritative source — adapter IDs like
        'radio' are common config aliases for Meshtastic adapters.
        """
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="Meshtastic outbound queue is full; enqueue rejected (1/1)",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="radio",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="Meshtastic outbound queue is full; enqueue rejected (1/1)",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "meshtastic_queue_rejected"

    def test_unrelated_error_preserves_failure_kind(self) -> None:
        """Error without queue/full or enqueue-rejected patterns preserves
        original failure_kind as failure_kind_detail."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="meshtastic",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "adapter_transient"

    def test_no_failure_kind_returns_none(self) -> None:
        """Receipt with no failure_kind produces None failure_kind_detail."""
        receipt = _make_receipt(status="sent")
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] is None

    # -- Meshtastic queue: error-pattern-based (no "meshtastic" required) --

    def test_radio_adapter_queue_full_enqueue_rejected(self) -> None:
        """target_adapter='radio' with queue-full enqueue-rejected error
        (no 'meshtastic' in text) → meshtastic_queue_rejected."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="queue is full; enqueue rejected (1/1)",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="radio",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="queue is full; enqueue rejected (1/1)",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "meshtastic_queue_rejected"

    def test_radio_adapter_explicit_meshtastic_queue_error(self) -> None:
        """target_adapter='radio' with explicit Meshtastic queue-full error
        → meshtastic_queue_rejected."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="Meshtastic outbound queue is full; enqueue rejected (1/1)",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="radio",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="Meshtastic outbound queue is full; enqueue rejected (1/1)",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "meshtastic_queue_rejected"

    def test_unrelated_queue_depth_preserves_failure_kind(self) -> None:
        """Error 'queue depth warning' without queue+full or enqueue-rejected
        preserves original failure_kind."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="queue depth warning",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="radio",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="queue depth warning",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "adapter_transient"

    def test_unrelated_full_payload_preserves_failure_kind(self) -> None:
        """Error 'full payload received' without queue+full or enqueue-rejected
        preserves original failure_kind."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="full payload received",
        )
        receipt = DeliveryReceipt(
            receipt_id=receipt.receipt_id,
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter="radio",
            target_channel="ch-0",
            route_id=receipt.route_id,
            status="failed",
            failure_kind="adapter_transient",
            error="full payload received",
            attempt_number=1,
            created_at=receipt.created_at,
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "adapter_transient"

    # -- E2EE: tightened to Matrix-specific patterns --

    def test_e2ee_matrix_room_encrypted_crypto_not_active(self) -> None:
        """Matrix-specific E2EE error → e2ee_blocked."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_permanent",
            error="Matrix room is encrypted but E2EE crypto is not active",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "e2ee_blocked"

    def test_e2ee_unable_to_decrypt_megolm(self) -> None:
        """unable to decrypt megolm event → e2ee_blocked."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_permanent",
            error="unable to decrypt megolm event",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "e2ee_blocked"

    def test_encrypted_packet_not_e2ee_blocked(self) -> None:
        """Generic 'encrypted packet' does NOT match E2EE patterns →
        preserves original failure_kind."""
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="encrypted packet",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind_detail"] == "adapter_transient"


# ===================================================================
# 1. Success explanation/report includes native message ID
# ===================================================================


class TestSuccessReportNativeMessageId:
    """Successful delivery produces a report dict with the adapter's
    native message ID."""

    def test_success_outcome_receipt_has_adapter_message_id(self) -> None:
        receipt = _make_receipt(
            status="sent",
            adapter_message_id="$native-matrix-evt-42",
        )
        outcome = _make_outcome(status="success", receipt=receipt)
        assert outcome.receipt is not None
        assert outcome.receipt.adapter_message_id == "$native-matrix-evt-42"

    def test_adapter_delivery_result_native_id(self) -> None:
        """AdapterDeliveryResult carries native_message_id from platform."""
        result = AdapterDeliveryResult(
            native_message_id="$plat-123",
            native_channel_id="!room:test",
        )
        assert result.native_message_id == "$plat-123"

    def test_success_outcome_serializable_with_native_id(self) -> None:
        """Outcome with receipt + native ID is msgspec-JSON-serializable."""
        receipt = _make_receipt(
            status="sent",
            adapter_message_id="$ser-001",
        )
        outcome = _make_outcome(status="success", receipt=receipt)
        parsed = msgspec.json.decode(msgspec.json.encode(outcome.receipt))
        assert parsed["adapter_message_id"] == "$ser-001"

    def test_success_receipt_without_adapter_message_id(self) -> None:
        """Success receipt with no native message ID (queue-based adapter)."""
        receipt = _make_receipt(
            status="sent",
            adapter_message_id=None,
        )
        outcome = _make_outcome(status="success", receipt=receipt)
        assert outcome.status == "success"
        assert outcome.receipt is not None
        assert outcome.receipt.adapter_message_id is None


# ===================================================================
# 2. Transient failure: retryable=true / adapter_transient
# ===================================================================


class TestTransientFailureClassification:
    """Transient failures are classified as adapter_transient (retryable)."""

    def test_adapter_send_error_transient_classifies_correctly(self) -> None:
        err = AdapterSendError("timeout", transient=True)
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT
        assert kind.is_retryable is True

    def test_timeout_error_classifies_as_transient(self) -> None:
        kind = RetryExecutor.classify_failure(TimeoutError("timed out"))
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_connection_error_classifies_as_transient(self) -> None:
        kind = RetryExecutor.classify_failure(ConnectionError("refused"))
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_transient_failure_outcome_report(self) -> None:
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
        )
        outcome = _make_outcome(
            status="transient_failure",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT,
            receipt=receipt,
            error="ConnectionError: timeout",
        )
        assert outcome.failure_kind is DeliveryFailureKind.ADAPTER_TRANSIENT
        assert outcome.failure_kind is not None
        assert outcome.failure_kind.is_retryable is True
        assert outcome.receipt is not None
        assert outcome.receipt.failure_kind == "adapter_transient"

    def test_transient_receipt_includes_retry_policy(self) -> None:
        """Failed receipt for transient error carries retry policy fields."""
        policy = RetryPolicy(max_attempts=5, backoff_base=3.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-trans-1",
            delivery_plan_id="plan-trans",
            target_adapter="adapter-trans",
            previous_receipt_id=None,
            attempt_number=1,
            error="ConnectionError: timeout",
        )
        assert receipt.retry_max_attempts == 5
        assert receipt.retry_backoff_base == 3.0
        assert receipt.status == "failed"
        assert receipt.next_retry_at is not None


# ===================================================================
# 3. Permanent failure: retryable=false / adapter_permanent
# ===================================================================


class TestPermanentFailureClassification:
    """Permanent failures are classified as adapter_permanent (not retryable)."""

    def test_adapter_permanent_error_classifies(self) -> None:
        err = AdapterPermanentError("forbidden")
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT
        assert kind.is_retryable is False

    def test_runtime_error_classifies_as_permanent(self) -> None:
        kind = RetryExecutor.classify_failure(RuntimeError("malformed"))
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT

    def test_permanent_failure_outcome_report(self) -> None:
        receipt = _make_receipt(
            status="failed",
            failure_kind="adapter_permanent",
            error="ValueError: malformed payload",
        )
        outcome = _make_outcome(
            status="permanent_failure",
            failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
            receipt=receipt,
            error="ValueError: malformed payload",
        )
        assert outcome.failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT
        assert outcome.failure_kind is not None
        assert outcome.failure_kind.is_retryable is False
        assert outcome.receipt is not None
        assert outcome.receipt.failure_kind == "adapter_permanent"


# ===================================================================
# 4. Dead-letter exhaustion evidence
# ===================================================================


class TestDeadLetterExhaustionEvidence:
    """Dead-letter receipt carries retry exhaustion metadata."""

    def test_dead_letter_receipt_has_retry_policy_fields(self) -> None:
        policy = RetryPolicy(max_attempts=3, backoff_base=2.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-dl-unif",
            delivery_plan_id="plan-dl",
            target_adapter="adapter-dl",
            previous_receipt_id="rcpt-prev",
            attempt_number=4,
            error="Retry exhausted after 3 attempts",
        )
        assert receipt.status == "dead_lettered"
        assert receipt.attempt_number == 4
        assert receipt.retry_max_attempts == 3
        assert receipt.retry_backoff_base == 2.0
        assert receipt.next_retry_at is None
        assert "exhausted" in (receipt.error or "")

    def test_dead_letter_receipt_json_serializable(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-dl-json",
            delivery_plan_id="plan-dl-json",
            target_adapter="adapter-dl",
            previous_receipt_id=None,
            attempt_number=4,
            error="Retry exhausted",
        )
        parsed = msgspec.json.decode(msgspec.json.encode(receipt))
        assert parsed["status"] == "dead_lettered"
        assert parsed["retry_max_attempts"] == 3

    def test_dead_letter_receipt_json_no_secrets(self) -> None:
        """Dead-letter receipt JSON contains no secret values."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-dl-safe",
            delivery_plan_id="plan-dl-safe",
            target_adapter="adapter-safe",
            previous_receipt_id=None,
            attempt_number=4,
            error="Retry exhausted after 3 attempts",
        )
        raw = msgspec.json.encode(receipt).decode().lower()
        assert "access_token" not in raw
        assert "password" not in raw
        assert "tok_" not in raw
        assert "syt_" not in raw


# ===================================================================
# 5. Loop suppressed: DeliveryOutcome may be skipped; suppressed
#    receipts persisted where event/target context exists
# ===================================================================


class TestLoopSuppressedVisibility:
    """Loop suppression: DeliveryOutcome may be skipped (no receipt in
    the outcome object), but the pipeline persists a suppressed receipt
    (status="suppressed") when event/target context is available —
    covering loop_suppressed, capacity_rejection, and shutdown_rejection.

    duplicate_suppressed was removed from the enum (never emitted).
    Pre-storage dedup returns an empty outcomes list with no receipt.
    """

    def test_loop_suppressed_failure_kind_not_retryable(self) -> None:
        assert DeliveryFailureKind.LOOP_SUPPRESSED.is_retryable is False

    def test_loop_suppressed_enum_value(self) -> None:
        assert DeliveryFailureKind.LOOP_SUPPRESSED.value == "loop_suppressed"

    def test_skipped_outcome_without_receipt_helper_only(self) -> None:
        """Helper-constructed skipped outcome has no receipt by default.

        This tests the helper's default behaviour, not the pipeline contract.
        The pipeline may persist a separate suppressed receipt outside the
        outcome when event/target context exists."""
        outcome = _make_outcome(
            status="skipped",
            error="loop_prevented",
        )
        assert outcome.status == "skipped"
        assert outcome.receipt is None
        assert "loop" in (outcome.error or "")

    def test_loop_suppressed_outcome_with_suppressed_receipt(self) -> None:
        """Loop-suppressed DeliveryOutcome may carry a suppressed receipt.

        The pipeline persists suppressed receipts for loop/capacity/shutdown
        when event/target context exists.  Pre-storage dedup returns no
        receipt because no event has been stored yet."""
        receipt = _make_receipt(
            status="suppressed",
            failure_kind="loop_suppressed",
            error="Loop prevented delivery",
        )
        outcome = _make_outcome(
            status="skipped",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            receipt=receipt,
            error="Loop prevented delivery",
        )
        assert outcome.status == "skipped"
        assert outcome.receipt is not None
        assert outcome.receipt.status == "suppressed"
        assert outcome.receipt.failure_kind == "loop_suppressed"
        assert outcome.failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED

    def test_capacity_rejection_outcome_with_suppressed_receipt(self) -> None:
        """Capacity-rejection outcome may carry a suppressed receipt."""
        receipt = _make_receipt(
            status="suppressed",
            failure_kind="capacity_rejection",
            error="delivery_capacity_exceeded",
        )
        outcome = _make_outcome(
            status="skipped",
            receipt=receipt,
            error="delivery_capacity_exceeded",
        )
        assert outcome.receipt is not None
        assert outcome.receipt.status == "suppressed"

    def test_shutdown_rejection_outcome_with_suppressed_receipt(self) -> None:
        """Shutdown-rejection outcome may carry a suppressed receipt."""
        receipt = _make_receipt(
            status="suppressed",
            failure_kind="shutdown_rejection",
            error="Pipeline shutdown in progress",
        )
        outcome = _make_outcome(
            status="skipped",
            receipt=receipt,
            error="Pipeline shutdown in progress",
        )
        assert outcome.receipt is not None
        assert outcome.receipt.status == "suppressed"

    def test_suppressed_receipt_status_is_valid(self) -> None:
        """Pipeline persists receipts with status='suppressed' for
        loop/capacity/shutdown suppression where context exists."""
        receipt = _make_receipt(status="suppressed", failure_kind="loop_suppressed")
        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "loop_suppressed"

    def test_capacity_rejection_suppressed_receipt(self) -> None:
        """Capacity rejection produces a suppressed receipt."""
        receipt = _make_receipt(status="suppressed", failure_kind="capacity_rejection")
        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "capacity_rejection"

    def test_shutdown_rejection_suppressed_receipt(self) -> None:
        """Shutdown rejection produces a suppressed receipt."""
        receipt = _make_receipt(status="suppressed", failure_kind="shutdown_rejection")
        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "shutdown_rejection"


# ===================================================================
# 6. DUPLICATE_SUPPRESSED: removed from enum (Tranche 6)
# ===================================================================

# DUPLICATE_SUPPRESSED was removed from DeliveryFailureKind because it was
# never emitted at runtime.  Duplicate native-ref suppression returns an
# empty outcomes list before storage; no receipt is persisted.


# ===================================================================
# 7. Matrix success metadata includes matrix_txn_id
# ===================================================================


class TestMatrixTxnIdInSuccess:
    """Matrix adapter deliver() returns metadata with matrix_txn_id."""

    async def test_matrix_delivery_result_has_txn_id(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter, _matrix_txn_id

        config = _matrix_config()
        adapter = MatrixAdapter(config)
        mock_session = MagicMock()
        mock_session.room_send = AsyncMock(return_value=_mock_send_response())
        adapter._session = mock_session

        result = _matrix_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        assert "matrix_txn_id" in delivery.metadata
        room_id = result.target_channel or "!room:example.com"
        expected_txn = _matrix_txn_id(result, room_id)
        assert delivery.metadata["matrix_txn_id"] == expected_txn


# ===================================================================
# 8. Matrix E2EE blocked is permanent/recognizable
# ===================================================================


class TestMatrixE2EEBlockedPermanent:
    """Encrypted-room sends blocked when E2EE disabled → permanent."""

    async def test_e2ee_blocked_raises_permanent(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        config = _matrix_config()
        adapter = MatrixAdapter(config)

        mock_session = MagicMock()
        mock_session.crypto_enabled = False
        mock_session.room_state.return_value = "encrypted"
        adapter._session = mock_session

        result = _matrix_result()
        with pytest.raises(AdapterPermanentError, match="encrypted but E2EE"):
            await adapter.deliver(result)

    async def test_e2ee_error_classifies_as_permanent(self) -> None:
        """E2EE-blocked AdapterPermanentError classifies as ADAPTER_PERMANENT."""
        err = AdapterPermanentError("encrypted but E2EE crypto is not active")
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT
        assert kind.is_retryable is False


# ===================================================================
# 9. Meshtastic queue full/rejected is transient
# ===================================================================


class TestMeshtasticQueueRejectedTransient:
    """Meshtastic queue full rejection evidence.

    MeshtasticSendError is NOT a subclass of AdapterSendError; the adapter
    catches it and wraps to AdapterSendError(transient=True) at the boundary.
    RetryExecutor.classify_failure only recognises AdapterSendError for the
    transient/permanent split, so an unwrapped MeshtasticSendError falls
    through to ADAPTER_PERMANENT.
    """

    def test_queue_full_error_is_transient(self) -> None:
        err = MeshtasticSendError("queue is full", transient=True)
        assert err.transient is True

    def test_queue_full_adapter_wrapped_classifies_as_transient(self) -> None:
        """Adapter wraps queue-full MeshtasticSendError as
        AdapterSendError(transient=True), which classifies as ADAPTER_TRANSIENT."""
        err = AdapterSendError("queue is full", transient=True)
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT
        assert kind.is_retryable is True

    def test_meshtastic_send_error_unwrapped_classifies_as_permanent(self) -> None:
        """Unwrapped MeshtasticSendError is not recognised by classify_failure
        (it is not an AdapterSendError), so it falls through to ADAPTER_PERMANENT."""
        err = MeshtasticSendError("queue is full", transient=True)
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT

    async def test_queue_full_rejection_increments_counter(self) -> None:
        q = MeshtasticOutboundQueue(max_queue_size=1)
        await q.enqueue({"text": "first"}, channel_index=0)
        with pytest.raises(MeshtasticSendError, match="queue is full"):
            await q.enqueue({"text": "overflow"}, channel_index=0)
        assert q.total_rejected == 1

    def test_meshtastic_send_error_is_not_adapter_send_error(self) -> None:
        """MeshtasticSendError has its own hierarchy (MeshtasticError → Exception).
        The adapter wraps it to AdapterSendError at the delivery boundary."""
        assert not issubclass(MeshtasticSendError, AdapterSendError)
        assert issubclass(MeshtasticSendError, Exception)


# ===================================================================
# 10. Meshtastic classifier diagnostics counters present
# ===================================================================


class TestMeshtasticClassifierDiagnosticsCounters:
    """Meshtastic adapter diagnostics expose aggregate inbound classifier
    counters: ignored, dropped, deferred."""

    async def test_diagnostics_has_all_classifier_keys(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="cls-diag", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="cls-diag",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            classifier_keys = [
                "classifier_packets_seen",
                "classifier_packets_relayed",
                "classifier_packets_ignored",
                "classifier_packets_dropped",
                "classifier_packets_deferred",
            ]
            for key in classifier_keys:
                assert key in diag, f"Missing classifier key: {key}"
        finally:
            await adapter.stop()

    async def test_diagnostics_has_queue_total_rejected(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="rej-diag", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = AdapterContext(
            adapter_id="rej-diag",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert "queue_total_rejected" in diag
            assert isinstance(diag["queue_total_rejected"], int)
        finally:
            await adapter.stop()


# ===================================================================
# 11. JSON/evidence output secret safety
# ===================================================================


class TestEvidenceSecretSafety:
    """Evidence and receipt JSON output must never contain secrets."""

    _SECRET_SUBSTRINGS = ("access_token", "password", "tok_", "syt_", "sk_")

    def test_receipt_dict_no_secret_keys(self) -> None:
        import typing

        hints = typing.get_type_hints(DeliveryReceipt)
        for secret in ("access_token", "token", "password", "secret", "api_key"):
            assert secret not in hints, f"DeliveryReceipt has secret key: {secret}"

    def test_dead_letter_receipt_json_safe(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-secret-1",
            delivery_plan_id="plan-secret",
            target_adapter="adapter-secret",
            previous_receipt_id=None,
            attempt_number=4,
            error="Retry exhausted",
        )
        raw = msgspec.json.encode(receipt).decode().lower()
        for substr in self._SECRET_SUBSTRINGS:
            assert substr not in raw, f"Secret substring '{substr}' in receipt JSON"

    def test_retry_receipt_json_safe(self) -> None:
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-secret-retry",
            delivery_plan_id="plan-secret-retry",
            target_adapter="adapter-secret-retry",
            previous_receipt_id=None,
            attempt_number=1,
            error="ConnectionError: timeout",
        )
        raw = msgspec.json.encode(receipt).decode().lower()
        for substr in self._SECRET_SUBSTRINGS:
            assert substr not in raw, f"Secret substring '{substr}' in receipt JSON"

    async def test_matrix_delivery_metadata_no_secrets(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        config = _matrix_config()
        adapter = MatrixAdapter(config)
        mock_session = MagicMock()
        mock_session.room_send = AsyncMock(return_value=_mock_send_response())
        adapter._session = mock_session

        result = _matrix_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        meta = dict(delivery.metadata)
        assert "access_token" not in meta
        assert "token" not in meta
        assert "password" not in meta
        assert "secret" not in meta
        # matrix_txn_id is allowed
        assert "matrix_txn_id" in meta


# ===================================================================
# 12. Failure kind / status consistency
# ===================================================================


class TestFailureKindStatusConsistency:
    """All valid failure_kind/status combinations produce consistent
    is_retryable classification."""

    def test_transient_failure_kind_with_transient_status(self) -> None:
        """transient_failure status with ADAPTER_TRANSIENT is retryable."""
        outcome = _make_outcome(
            status="transient_failure",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT,
            error="ConnectionError: timeout",
        )
        assert outcome.failure_kind is not None
        assert outcome.failure_kind.is_retryable is True
        assert outcome.status == "transient_failure"

    def test_permanent_failure_kind_with_permanent_status(self) -> None:
        """permanent_failure status with ADAPTER_PERMANENT is not retryable."""
        outcome = _make_outcome(
            status="permanent_failure",
            failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
            error="ValueError: malformed",
        )
        assert outcome.failure_kind is not None
        assert outcome.failure_kind.is_retryable is False
        assert outcome.status == "permanent_failure"

    def test_all_non_transient_kinds_are_not_retryable(self) -> None:
        """Every failure kind except ADAPTER_TRANSIENT is not retryable."""
        non_transient = [
            k
            for k in DeliveryFailureKind
            if k is not DeliveryFailureKind.ADAPTER_TRANSIENT
        ]
        for kind in non_transient:
            assert kind.is_retryable is False, f"{kind.name} should not be retryable"

    def test_skipped_status_never_has_retryable_kind(self) -> None:
        """Skipped outcomes should not have a retryable failure_kind."""
        assert DeliveryFailureKind.LOOP_SUPPRESSED.is_retryable is False


# ---------------------------------------------------------------------------
# Tests: suppressed-only incident summary classification
# (moved from test_evidence_cli.py)
# ---------------------------------------------------------------------------


class TestSuppressedIncidentSummary:
    """Incident summary for suppressed-only receipts with failure_kind."""

    @pytest.mark.asyncio
    async def test_suppressed_loop_only_is_permanent(self, config_fake: Path) -> None:
        """Suppressed receipt with failure_kind=loop_suppressed → permanent."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_suppressed(
            db_path,
            event_id="ev-supp-loop-001",
            failure_kind="loop_suppressed",
        )

        report = await collect_evidence_bundle(
            str(config_fake),
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["classification"] == "permanent", (
            f"Expected 'permanent' for loop_suppressed, got "
            f"{summary['classification']!r}"
        )
        assert summary["first_failure_kind"] == "loop_suppressed"
        assert (
            summary["failed_count"] == 0
        ), "suppressed receipts must not increment failed_count"

    @pytest.mark.asyncio
    async def test_suppressed_capacity_only_is_operational(
        self, config_fake: Path
    ) -> None:
        """Suppressed receipt with failure_kind=capacity_rejection → operational."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_suppressed(
            db_path,
            event_id="ev-supp-cap-001",
            failure_kind="capacity_rejection",
            error="delivery_capacity_exceeded",
        )

        report = await collect_evidence_bundle(
            str(config_fake),
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["classification"] == "operational", (
            f"Expected 'operational' for capacity_rejection, got "
            f"{summary['classification']!r}"
        )
        assert summary["first_failure_kind"] == "capacity_rejection"
        assert summary["failed_count"] == 0

    @pytest.mark.asyncio
    async def test_suppressed_shutdown_only_is_operational(
        self, config_fake: Path
    ) -> None:
        """Suppressed receipt with failure_kind=shutdown_rejection → operational."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_suppressed(
            db_path,
            event_id="ev-supp-shutdown-001",
            failure_kind="shutdown_rejection",
            error="delivery_rejected_shutdown",
        )

        report = await collect_evidence_bundle(
            str(config_fake),
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["classification"] == "operational", (
            f"Expected 'operational' for shutdown_rejection, got "
            f"{summary['classification']!r}"
        )
        assert summary["first_failure_kind"] == "shutdown_rejection"
        assert summary["failed_count"] == 0

    @pytest.mark.asyncio
    async def test_suppressed_count_increments(self, config_fake: Path) -> None:
        """suppressed_count is populated correctly for suppressed-only events."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_suppressed(
            db_path,
            event_id="ev-supp-count-001",
            failure_kind="loop_suppressed",
        )

        report = await collect_evidence_bundle(
            str(config_fake),
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert (
            summary["suppressed_count"] == 1
        ), f"Expected suppressed_count=1, got {summary['suppressed_count']}"
        assert summary["failed_count"] == 0
        assert summary["sent_count"] == 0

    @pytest.mark.asyncio
    async def test_suppressed_recommended_commands_match_classification(
        self, config_fake: Path
    ) -> None:
        """Recommended commands match the classification for suppressed events."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_suppressed(
            db_path,
            event_id="ev-supp-cmds-001",
            failure_kind="loop_suppressed",
        )

        report = await collect_evidence_bundle(
            str(config_fake),
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        cmds = summary["recommended_commands"]
        assert len(cmds) > 0, "Expected non-empty recommended_commands"
        cmd_text = " ".join(cmds)
        # permanent classification recommends inspect/evidence commands
        assert (
            "inspect" in cmd_text
        ), f"Expected 'inspect' in recommended commands for permanent: {cmds}"

    @pytest.mark.asyncio
    async def test_suppressed_not_success(self, config_fake: Path) -> None:
        """Suppressed-only events are NEVER classified as success."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_suppressed(
            db_path,
            event_id="ev-supp-not-success-001",
            failure_kind="loop_suppressed",
        )

        report = await collect_evidence_bundle(
            str(config_fake),
            event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert (
            summary["classification"] != "success"
        ), "Suppressed-only events must not be classified as 'success'"
