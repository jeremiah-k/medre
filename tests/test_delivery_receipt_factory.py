"""Unit tests for :mod:`medre.core.engine.pipeline.receipt_factory`.

Covers:
* identity fields set from explicit args
* deterministic and generated ``created_at`` (timezone-aware)
* retry policy fields preserved exactly
* ``source`` and ``replay_run_id`` preserved
* ``attempt_number`` and ``parent_receipt_id`` preserved
* ``rendering_evidence`` preserved exactly
* error/failure_kind passed through without classification
* no storage/persistence imports in the module
* generated ``receipt_id`` format and uniqueness
"""

from __future__ import annotations

import importlib
import inspect
from datetime import datetime, timezone

from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events.canonical import DeliveryReceipt

# -- Helpers -----------------------------------------------------------------


def _base_kwargs(**overrides: object) -> dict:
    """Return minimal valid kwargs, with optional overrides."""
    kw: dict = {
        "event_id": "evt-001",
        "delivery_plan_id": "plan-001",
        "target_adapter": "matrix",
        "target_channel": "!room:server",
        "route_id": "route-001",
        "status": "queued",
    }
    kw.update(overrides)
    return kw


# -- Tests -------------------------------------------------------------------


class TestIdentityFields:
    """Explicit caller-supplied fields are passed through unchanged."""

    def test_event_id(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(event_id="evt-42"))
        assert r.event_id == "evt-42"

    def test_delivery_plan_id(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(delivery_plan_id="plan-99"))
        assert r.delivery_plan_id == "plan-99"

    def test_target_adapter(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(target_adapter="meshtastic"))
        assert r.target_adapter == "meshtastic"

    def test_target_channel(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(target_channel="#general"))
        assert r.target_channel == "#general"

    def test_route_id(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(route_id="route-abc"))
        assert r.route_id == "route-abc"

    def test_status(self) -> None:
        for s in ("queued", "sent", "failed", "dead_lettered", "suppressed"):
            r = build_delivery_receipt(**_base_kwargs(status=s))
            assert r.status == s

    def test_returns_delivery_receipt(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert isinstance(r, DeliveryReceipt)


class TestCreatedAt:
    """created_at handling: deterministic when provided, timezone-aware when generated."""

    def test_deterministic_created_at(self) -> None:
        ts = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        r = build_delivery_receipt(**_base_kwargs(created_at=ts))
        assert r.created_at == ts
        assert r.created_at.tzinfo is not None

    def test_generated_created_at_is_timezone_aware(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.created_at.tzinfo is not None

    def test_generated_created_at_is_utc(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.created_at.tzinfo == timezone.utc


class TestRetryPolicyFields:
    """Retry policy fields are preserved exactly as provided."""

    def test_retry_max_attempts(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(retry_max_attempts=5))
        assert r.retry_max_attempts == 5

    def test_retry_backoff_base(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(retry_backoff_base=2.5))
        assert r.retry_backoff_base == 2.5

    def test_retry_max_delay(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(retry_max_delay=60.0))
        assert r.retry_max_delay == 60.0

    def test_retry_jitter(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(retry_jitter=True))
        assert r.retry_jitter is True

    def test_all_retry_fields_together(self) -> None:
        r = build_delivery_receipt(
            **_base_kwargs(
                retry_max_attempts=3,
                retry_backoff_base=1.0,
                retry_max_delay=30.0,
                retry_jitter=False,
            )
        )
        assert r.retry_max_attempts == 3
        assert r.retry_backoff_base == 1.0
        assert r.retry_max_delay == 30.0
        assert r.retry_jitter is False

    def test_retry_fields_default_none(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.retry_max_attempts is None
        assert r.retry_backoff_base is None
        assert r.retry_max_delay is None
        assert r.retry_jitter is None


class TestSourceAndReplay:
    """source and replay_run_id are preserved."""

    def test_source_live(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(source="live"))
        assert r.source == "live"

    def test_source_retry(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(source="retry"))
        assert r.source == "retry"

    def test_source_replay(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(source="replay"))
        assert r.source == "replay"

    def test_replay_run_id(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(replay_run_id="run-abc"))
        assert r.replay_run_id == "run-abc"

    def test_replay_run_id_default_none(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.replay_run_id is None


class TestAttemptAndParent:
    """attempt_number and parent_receipt_id are preserved."""

    def test_attempt_number(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(attempt_number=3))
        assert r.attempt_number == 3

    def test_attempt_number_default_one(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.attempt_number == 1

    def test_parent_receipt_id(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(parent_receipt_id="rcpt-prev"))
        assert r.parent_receipt_id == "rcpt-prev"

    def test_parent_receipt_id_default_none(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.parent_receipt_id is None


class TestRenderingEvidence:
    """rendering_evidence is preserved exactly."""

    def test_rendering_evidence_string(self) -> None:
        evidence = "text/plain:42chars:sha256=abc123"
        r = build_delivery_receipt(**_base_kwargs(rendering_evidence=evidence))
        assert r.rendering_evidence == evidence

    def test_rendering_evidence_default_none(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.rendering_evidence is None


class TestErrorPassthrough:
    """error and failure_kind are passed through without classification."""

    def test_error_preserved(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(error="connection refused"))
        assert r.error == "connection refused"

    def test_failure_kind_preserved(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(failure_kind="transient_network"))
        assert r.failure_kind == "transient_network"

    def test_no_exception_type_input_api(self) -> None:
        """The function signature accepts only str | None for error/failure_kind,
        never an exception type.  Verify the parameter types are string-based."""
        sig = inspect.signature(build_delivery_receipt)
        error_param = sig.parameters["error"]
        failure_kind_param = sig.parameters["failure_kind"]
        # The annotations should be str | None, not Exception or similar.
        assert "Exception" not in str(error_param.annotation)
        assert "Exception" not in str(failure_kind_param.annotation)


class TestNoPersistenceImports:
    """The module must not import storage, persistence, or lifecycle modules."""

    def test_no_storage_imports(self) -> None:
        mod = importlib.import_module("medre.core.engine.pipeline.receipt_factory")
        source_lines = [
            line.strip()
            for line in inspect.getsource(mod).splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        forbidden = [
            "storage",
            "persistence",
            "lifecycle",
            "retry",
            "planning",
            "adapter",
        ]
        for line in source_lines:
            for word in forbidden:
                assert not (
                    line.startswith(f"import {word}") or line.startswith(f"from {word}")
                ), f"Forbidden import involving '{word}': {line!r}"

    def test_minimal_imports_only(self) -> None:
        """Module should import only datetime, timezone, uuid, typing, and
        DeliveryReceipt."""
        mod = importlib.import_module("medre.core.engine.pipeline.receipt_factory")
        source_lines = [
            line.strip()
            for line in inspect.getsource(mod).splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        # Allowed import sources
        allowed_prefixes = (
            "import uuid",
            "from __future__",
            "from datetime",
            "from typing",
            "from medre.core.events.canonical",
        )
        for line in source_lines:
            assert line.startswith(
                allowed_prefixes
            ), f"Unexpected import line: {line!r}"


class TestReceiptId:
    """Generated receipt_id format and uniqueness."""

    def test_generated_receipt_id_starts_with_rcpt(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.receipt_id.startswith("rcpt-")

    def test_generated_receipt_id_non_empty(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert len(r.receipt_id) > len("rcpt-")

    def test_two_generated_receipts_have_different_ids(self) -> None:
        r1 = build_delivery_receipt(**_base_kwargs())
        r2 = build_delivery_receipt(**_base_kwargs())
        assert r1.receipt_id != r2.receipt_id

    def test_explicit_receipt_id_preserved(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(receipt_id="rcpt-custom-123"))
        assert r.receipt_id == "rcpt-custom-123"


class TestSequenceDefault:
    """sequence defaults to 0 but can be overridden."""

    def test_sequence_default_zero(self) -> None:
        r = build_delivery_receipt(**_base_kwargs())
        assert r.sequence == 0

    def test_sequence_explicit(self) -> None:
        r = build_delivery_receipt(**_base_kwargs(sequence=7))
        assert r.sequence == 7
