"""Tests for the canonical runtime evidence failure taxonomy.

Validates that:
* All 11 ``DeliveryFailureKind`` values map to ``FailureTaxon`` members.
* ``derive_failure_kind_detail`` matches ``runtime.reporting`` for
  representative error patterns (Matrix E2EE, Meshtastic queue full,
  listen_only suppression, shutdown_drain_timeout, route policy denied).
* ``compute_retryable`` matches ``runtime.reporting`` for all status /
  failure_kind / next_retry_at combinations.
* ``resolve_taxon`` produces the expected refined taxon for each case.
* ``taxon_category`` returns consistent coarse categories.
* All ``FailureTaxon`` values are JSON-safe plain strings.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.evidence.failure_taxonomy import (
    FAILURE_KIND_TO_TAXON,
    FailureTaxon,
    compute_retryable,
    derive_failure_kind_detail,
    resolve_taxon,
    taxon_category,
)
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.runtime.reporting import _compute_retryable as reporting_compute_retryable
from medre.runtime.reporting import (
    _derive_failure_kind_detail as reporting_derive_failure_kind_detail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A fixed datetime for deterministic next_retry_at comparisons.
_RETRY_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. DeliveryFailureKind coverage
# ---------------------------------------------------------------------------


class TestDeliveryFailureKindCoverage:
    """Every ``DeliveryFailureKind`` value must map to a ``FailureTaxon``."""

    @pytest.fixture(params=list(DeliveryFailureKind))
    def failure_kind(self, request: pytest.FixtureRequest) -> DeliveryFailureKind:
        return request.param

    def test_every_kind_in_mapping(self, failure_kind: DeliveryFailureKind) -> None:
        assert failure_kind.value in FAILURE_KIND_TO_TAXON

    def test_mapping_value_matches_kind_string(
        self,
        failure_kind: DeliveryFailureKind,
    ) -> None:
        taxon = FAILURE_KIND_TO_TAXON[failure_kind.value]
        assert taxon.value == failure_kind.value

    def test_resolve_taxon_for_each_kind(
        self,
        failure_kind: DeliveryFailureKind,
    ) -> None:
        taxon = resolve_taxon(failure_kind.value, error=None)
        assert taxon is not None
        assert taxon == FAILURE_KIND_TO_TAXON[failure_kind.value]


# ---------------------------------------------------------------------------
# 2. derive_failure_kind_detail — consistency with reporting
# ---------------------------------------------------------------------------


class TestDeriveFailureKindDetail:
    """Compare taxonomy ``derive_failure_kind_detail`` to reporting helper."""

    @pytest.mark.parametrize(
        ("failure_kind", "error"),
        [
            # None / empty inputs
            (None, None),
            (None, "something"),
            ("", None),
            ("", "some error"),
            # Shutdown drain timeout
            (
                "shutdown_rejection",
                "delivery abandoned: shutdown_drain_timeout reached",
            ),
            # Route policy denied
            ("policy_suppressed", "route policy denied: target blocked"),
            ("policy_suppressed", "capability_suppressed: not relevant here"),
            # E2EE / Matrix encrypted
            ("adapter_permanent", "E2EE decryption failed for room"),
            ("adapter_permanent", "megolm session not found"),
            ("adapter_permanent", "olm session error"),
            ("adapter_permanent", "unable to decrypt message"),
            ("adapter_permanent", "crypto is not active"),
            ("adapter_permanent", "matrix room is encrypted"),
            ("adapter_permanent", "room is encrypted but e2ee is disabled"),
            # Meshtastic queue full
            ("adapter_transient", "outbound queue is full, cannot enqueue"),
            ("adapter_transient", "enqueue rejected: capacity exceeded"),
            # Listen-only suppression
            ("capability_suppressed", "outbound suppressed: listen_only mode active"),
            # Queue drain cancelled
            ("shutdown_rejection", "queue drain cancelled during shutdown"),
            ("shutdown_rejection", "queue abandoned: process terminating"),
            # Default pass-through
            ("adapter_permanent", "unknown error text"),
            ("adapter_transient", "timeout connecting to host"),
            ("capability_suppressed", "capability_suppressed: reactions unsupported"),
            ("loop_suppressed", "self-loop detected"),
        ],
    )
    def test_matches_reporting(
        self,
        failure_kind: str | None,
        error: str | None,
    ) -> None:
        expected = reporting_derive_failure_kind_detail(failure_kind, error)
        actual = derive_failure_kind_detail(failure_kind, error)
        assert actual == expected

    def test_e2ee_not_triggered_by_generic_encrypted(self) -> None:
        """Generic 'encrypted' alone must NOT trigger e2ee_blocked."""
        fk = "adapter_permanent"
        error = "encrypted packet dropped"
        assert derive_failure_kind_detail(fk, error) != "e2ee_blocked"
        # Must match reporting too.
        assert derive_failure_kind_detail(
            fk, error
        ) == reporting_derive_failure_kind_detail(fk, error)


# ---------------------------------------------------------------------------
# 3. compute_retryable — consistency with reporting
# ---------------------------------------------------------------------------


class TestComputeRetryable:
    """Compare taxonomy ``compute_retryable`` to reporting helper."""

    @pytest.mark.parametrize(
        ("failure_kind", "status", "next_retry_at", "expected"),
        [
            # dead_lettered → always False
            ("adapter_transient", "dead_lettered", None, False),
            ("adapter_transient", "dead_lettered", _RETRY_AT, False),
            ("adapter_permanent", "dead_lettered", None, False),
            # suppressed → always False
            ("capability_suppressed", "suppressed", None, False),
            ("loop_suppressed", "suppressed", _RETRY_AT, False),
            # next_retry_at set → True
            ("adapter_transient", "failed", _RETRY_AT, True),
            ("adapter_permanent", "failed", _RETRY_AT, True),
            ("unknown", "failed", _RETRY_AT, True),
            # failed + adapter_transient, no next_retry_at → True
            ("adapter_transient", "failed", None, True),
            # failed + non-transient, no next_retry_at → False
            ("adapter_permanent", "failed", None, False),
            ("capability_suppressed", "failed", None, False),
            # sent / queued → False
            (None, "sent", None, False),
            (None, "queued", None, False),
            # None inputs
            (None, "failed", None, False),
        ],
    )
    def test_matches_reporting(
        self,
        failure_kind: str | None,
        status: str,
        next_retry_at: datetime | None,
        expected: bool,
    ) -> None:
        assert compute_retryable(failure_kind, status, next_retry_at) == expected
        assert compute_retryable(
            failure_kind, status, next_retry_at
        ) == reporting_compute_retryable(failure_kind, status, next_retry_at)


# ---------------------------------------------------------------------------
# 4. resolve_taxon — derived taxa
# ---------------------------------------------------------------------------


class TestResolveTaxon:
    """Verify ``resolve_taxon`` produces expected refined taxa."""

    @pytest.mark.parametrize(
        ("failure_kind", "error", "status", "expected_taxon"),
        [
            # Direct DeliveryFailureKind mappings
            ("planner_failure", None, "failed", FailureTaxon.PLANNER_FAILURE),
            ("renderer_failure", None, "failed", FailureTaxon.RENDERER_FAILURE),
            ("adapter_transient", "timeout", "failed", FailureTaxon.ADAPTER_TRANSIENT),
            (
                "adapter_permanent",
                "bad payload",
                "failed",
                FailureTaxon.ADAPTER_PERMANENT,
            ),
            ("adapter_missing", None, "failed", FailureTaxon.ADAPTER_MISSING),
            ("deadline_exceeded", None, "failed", FailureTaxon.DEADLINE_EXCEEDED),
            ("capacity_rejection", None, "failed", FailureTaxon.CAPACITY_REJECTION),
            ("shutdown_rejection", None, "failed", FailureTaxon.SHUTDOWN_REJECTION),
            (
                "loop_suppressed",
                "self-loop",
                "suppressed",
                FailureTaxon.LOOP_SUPPRESSED,
            ),
            (
                "policy_suppressed",
                "denied",
                "suppressed",
                FailureTaxon.POLICY_SUPPRESSED,
            ),
            (
                "capability_suppressed",
                "unsupported",
                "suppressed",
                FailureTaxon.CAPABILITY_SUPPRESSED,
            ),
            # dead_lettered → RETRY_EXHAUSTED (regardless of kind)
            (
                "adapter_transient",
                "timeout",
                "dead_lettered",
                FailureTaxon.RETRY_EXHAUSTED,
            ),
            (
                "adapter_permanent",
                "perm fail",
                "dead_lettered",
                FailureTaxon.RETRY_EXHAUSTED,
            ),
            # Derived: shutdown_drain_timeout → SHUTDOWN_PENDING
            (
                "shutdown_rejection",
                "shutdown_drain_timeout exceeded",
                "failed",
                FailureTaxon.SHUTDOWN_PENDING,
            ),
            # Derived: route policy denied → still POLICY_SUPPRESSED via detail
            (
                "policy_suppressed",
                "route policy denied",
                "suppressed",
                FailureTaxon.POLICY_SUPPRESSED,
            ),
            # Derived: listen_only → ROUTE_LISTEN_ONLY
            (
                "capability_suppressed",
                "outbound suppressed: listen_only",
                "suppressed",
                FailureTaxon.ROUTE_LISTEN_ONLY,
            ),
            # Derived: e2ee → DELIVERY_FAILED
            (
                "adapter_permanent",
                "E2EE decryption failed",
                "failed",
                FailureTaxon.DELIVERY_FAILED,
            ),
            # Derived: queue full → UNAVAILABLE
            ("adapter_transient", "queue is full", "failed", FailureTaxon.UNAVAILABLE),
            # Derived: queue drain cancelled → CANCELLED
            (
                "shutdown_rejection",
                "queue drain cancelled",
                "failed",
                FailureTaxon.CANCELLED,
            ),
            # No failure, success status → None
            (None, None, "sent", None),
            (None, None, "queued", None),
            # Suppressed without failure_kind → NOT_EXECUTED
            (None, "unknown reason", "suppressed", FailureTaxon.NOT_EXECUTED),
            # dead_lettered without failure_kind → RETRY_EXHAUSTED
            (None, None, "dead_lettered", FailureTaxon.RETRY_EXHAUSTED),
        ],
    )
    def test_resolve(
        self,
        failure_kind: str | None,
        error: str | None,
        status: str | None,
        expected_taxon: FailureTaxon | None,
    ) -> None:
        assert resolve_taxon(failure_kind, error, status) == expected_taxon


# ---------------------------------------------------------------------------
# 5. taxon_category — coarse buckets
# ---------------------------------------------------------------------------


class TestTaxonCategory:
    """Coarse categorisation covers all taxa."""

    @pytest.mark.parametrize(
        ("taxon", "expected_category"),
        [
            (FailureTaxon.ADAPTER_TRANSIENT, "retryable"),
            (FailureTaxon.PLANNER_FAILURE, "permanent"),
            (FailureTaxon.RENDERER_FAILURE, "permanent"),
            (FailureTaxon.ADAPTER_MISSING, "permanent"),
            (FailureTaxon.ADAPTER_PERMANENT, "permanent"),
            (FailureTaxon.LOOP_SUPPRESSED, "permanent"),
            (FailureTaxon.POLICY_SUPPRESSED, "permanent"),
            (FailureTaxon.CAPABILITY_SUPPRESSED, "permanent"),
            (FailureTaxon.DELIVERY_FAILED, "permanent"),
            (FailureTaxon.AUTH_FAILED, "permanent"),
            (FailureTaxon.NOT_CONFIGURED, "permanent"),
            (FailureTaxon.ROUTE_DISABLED, "permanent"),
            (FailureTaxon.ROUTE_LISTEN_ONLY, "permanent"),
            (FailureTaxon.CAPACITY_REJECTION, "operational"),
            (FailureTaxon.SHUTDOWN_REJECTION, "operational"),
            (FailureTaxon.DEADLINE_EXCEEDED, "operational"),
            (FailureTaxon.SHUTDOWN_PENDING, "operational"),
            (FailureTaxon.RETRY_EXHAUSTED, "derived_terminal"),
            (FailureTaxon.CANCELLED, "derived_terminal"),
            (FailureTaxon.NOT_EXECUTED, "derived_terminal"),
            (FailureTaxon.UNAVAILABLE, "derived_terminal"),
            (FailureTaxon.CONNECTION_FAILED, "derived_terminal"),
            (None, "unknown"),
        ],
    )
    def test_category(self, taxon: FailureTaxon | None, expected_category: str) -> None:
        assert taxon_category(taxon) == expected_category

    def test_all_taxa_classified(self) -> None:
        """Every FailureTaxon member must map to a known category."""
        for taxon in FailureTaxon:
            cat = taxon_category(taxon)
            assert cat in (
                "retryable",
                "permanent",
                "operational",
                "derived_terminal",
                "unknown",
            )


# ---------------------------------------------------------------------------
# 6. JSON safety
# ---------------------------------------------------------------------------


class TestJsonSafety:
    """All FailureTaxon values must be JSON-safe plain strings."""

    def test_all_values_are_strings(self) -> None:
        for taxon in FailureTaxon:
            assert isinstance(taxon.value, str)

    def test_no_empty_values(self) -> None:
        for taxon in FailureTaxon:
            assert taxon.value, f"{taxon.name} has an empty value"

    def test_values_are_lowercase_snake(self) -> None:
        """Values use lowercase_snake convention (no spaces, no special chars)."""
        for taxon in FailureTaxon:
            assert taxon.value == taxon.value.lower()
            assert " " not in taxon.value

    def test_serialisable_via_value(self) -> None:
        """``taxon.value`` can be used directly in JSON serialisation."""
        import json

        data = {t.name: t.value for t in FailureTaxon}
        serialized = json.dumps(data)
        assert isinstance(serialized, str)
        deserialized = json.loads(serialized)
        assert deserialized == data


# ---------------------------------------------------------------------------
# 7. Suppression kinds + adapter transient/permanent + capacity/shutdown
# ---------------------------------------------------------------------------


class TestSuppressionAndOperationalKinds:
    """Cover suppression, adapter, capacity, and shutdown scenarios."""

    def test_suppression_kinds_not_retryable(self) -> None:
        suppression_kinds = [
            "loop_suppressed",
            "policy_suppressed",
            "capability_suppressed",
        ]
        for fk in suppression_kinds:
            assert not compute_retryable(fk, "suppressed", None)
            assert not compute_retryable(fk, "suppressed", _RETRY_AT)

    def test_adapter_transient_retryable(self) -> None:
        assert compute_retryable("adapter_transient", "failed", None) is True
        assert compute_retryable("adapter_transient", "failed", _RETRY_AT) is True

    def test_adapter_permanent_not_retryable(self) -> None:
        assert compute_retryable("adapter_permanent", "failed", None) is False

    def test_capacity_rejection_not_retryable(self) -> None:
        assert compute_retryable("capacity_rejection", "failed", None) is False

    def test_shutdown_rejection_not_retryable(self) -> None:
        assert compute_retryable("shutdown_rejection", "failed", None) is False

    def test_deadline_exceeded_not_retryable(self) -> None:
        assert compute_retryable("deadline_exceeded", "failed", None) is False

    def test_unknown_failure_kind(self) -> None:
        assert resolve_taxon("totally_unknown_kind", "some error", "failed") is None

    def test_none_inputs(self) -> None:
        assert derive_failure_kind_detail(None, None) is None
        assert resolve_taxon(None, None, None) is None
        assert compute_retryable(None, "sent", None) is False


# ---------------------------------------------------------------------------
# 8. Representative detail strings
# ---------------------------------------------------------------------------


class TestRepresentativeDetails:
    """Cover the specific detail strings mentioned in the task."""

    def test_matrix_e2ee_detail(self) -> None:
        detail = derive_failure_kind_detail(
            "adapter_permanent", "E2EE decryption failed"
        )
        assert detail == "e2ee_blocked"
        taxon = resolve_taxon("adapter_permanent", "E2EE decryption failed", "failed")
        assert taxon == FailureTaxon.DELIVERY_FAILED

    def test_meshtastic_queue_full_detail(self) -> None:
        detail = derive_failure_kind_detail(
            "adapter_transient", "outbound queue is full"
        )
        assert detail == "meshtastic_queue_rejected"
        taxon = resolve_taxon("adapter_transient", "outbound queue is full", "failed")
        assert taxon == FailureTaxon.UNAVAILABLE

    def test_listen_only_suppression_detail(self) -> None:
        detail = derive_failure_kind_detail(
            "capability_suppressed",
            "outbound suppressed: device in listen_only mode",
        )
        assert detail == "meshtastic_outbound_suppressed"
        taxon = resolve_taxon(
            "capability_suppressed",
            "outbound suppressed: device in listen_only mode",
            "suppressed",
        )
        assert taxon == FailureTaxon.ROUTE_LISTEN_ONLY

    def test_shutdown_drain_timeout_detail(self) -> None:
        detail = derive_failure_kind_detail(
            "shutdown_rejection",
            "delivery abandoned: shutdown_drain_timeout reached",
        )
        assert detail == "shutdown_drain_timeout"
        taxon = resolve_taxon(
            "shutdown_rejection",
            "delivery abandoned: shutdown_drain_timeout reached",
            "failed",
        )
        assert taxon == FailureTaxon.SHUTDOWN_PENDING

    def test_route_policy_denied_detail(self) -> None:
        detail = derive_failure_kind_detail(
            "policy_suppressed",
            "route policy denied: target blocked by configuration",
        )
        assert detail == "policy_suppressed"
        taxon = resolve_taxon(
            "policy_suppressed",
            "route policy denied: target blocked by configuration",
            "suppressed",
        )
        assert taxon == FailureTaxon.POLICY_SUPPRESSED
