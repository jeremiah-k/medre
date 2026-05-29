"""Focused tests for delivery_state module.

Validates status vocabularies, terminal/claimable/accepted helpers,
transition tables, and edge cases.
"""

from __future__ import annotations

import pytest

from medre.core.engine.pipeline.delivery_state import (
    ACCEPTED_OUTCOME_STATUSES,
    ADAPTER_DELIVERY_STATUSES,
    CLAIMABLE_OUTBOX_STATUSES,
    OUTBOX_STATUSES,
    OUTBOX_TRANSITIONS,
    OUTCOME_STATUSES,
    RECEIPT_STATUSES,
    RECEIPT_TRANSITIONS,
    TERMINAL_OUTBOX_STATUSES,
    TERMINAL_RECEIPT_STATUSES,
    is_accepted_outcome_status,
    is_claimable_outbox_status,
    is_terminal_outbox_status,
    is_terminal_receipt_status,
    is_valid_queued_to_sent_transition,
    validate_outbox_status,
    validate_outbox_transition,
    validate_outcome_status,
    validate_receipt_status,
    validate_receipt_transition,
)

# ---------------------------------------------------------------------------
# Status vocabulary membership
# ---------------------------------------------------------------------------


class TestReceiptStatuses:
    """RECEIPT_STATUSES contains exactly the expected receipt statuses."""

    EXPECTED = {"queued", "sent", "failed", "dead_lettered", "suppressed"}

    def test_all_present(self) -> None:
        assert RECEIPT_STATUSES == self.EXPECTED

    @pytest.mark.parametrize("status", sorted(EXPECTED))
    def test_validate_receipt_status_known(self, status: str) -> None:
        assert validate_receipt_status(status) is True

    def test_validate_receipt_status_unknown(self) -> None:
        assert validate_receipt_status("unknown") is False

    def test_validate_receipt_status_empty(self) -> None:
        assert validate_receipt_status("") is False


class TestOutboxStatuses:
    """OUTBOX_STATUSES contains exactly the expected outbox statuses."""

    EXPECTED = {
        "pending",
        "in_progress",
        "queued",
        "sent",
        "retry_wait",
        "dead_lettered",
        "cancelled",
        "abandoned",
    }

    def test_all_present(self) -> None:
        assert OUTBOX_STATUSES == self.EXPECTED

    @pytest.mark.parametrize("status", sorted(EXPECTED))
    def test_validate_outbox_status_known(self, status: str) -> None:
        assert validate_outbox_status(status) is True

    def test_validate_outbox_status_unknown(self) -> None:
        assert validate_outbox_status("unknown") is False


class TestOutcomeStatuses:
    """OUTCOME_STATUSES contains exactly the expected outcome statuses."""

    EXPECTED = {
        "success",
        "queued",
        "transient_failure",
        "permanent_failure",
        "skipped",
    }

    def test_all_present(self) -> None:
        assert OUTCOME_STATUSES == self.EXPECTED

    @pytest.mark.parametrize("status", sorted(EXPECTED))
    def test_validate_outcome_status_known(self, status: str) -> None:
        assert validate_outcome_status(status) is True

    def test_validate_outcome_status_unknown(self) -> None:
        assert validate_outcome_status("bogus") is False


class TestAdapterDeliveryStatuses:
    """ADAPTER_DELIVERY_STATUSES contains exactly the adapter statuses."""

    EXPECTED = {"sent", "enqueued"}

    def test_all_present(self) -> None:
        assert ADAPTER_DELIVERY_STATUSES == self.EXPECTED


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


class TestTerminalReceiptStatus:
    """TERMINAL_RECEIPT_STATUSES and is_terminal_receipt_status."""

    EXPECTED_TERMINAL = {"sent", "dead_lettered", "suppressed"}
    EXPECTED_NON_TERMINAL = {"queued", "failed"}

    def test_terminal_set(self) -> None:
        assert TERMINAL_RECEIPT_STATUSES == self.EXPECTED_TERMINAL

    @pytest.mark.parametrize("status", sorted(EXPECTED_TERMINAL))
    def test_is_terminal_true(self, status: str) -> None:
        assert is_terminal_receipt_status(status) is True

    @pytest.mark.parametrize("status", sorted(EXPECTED_NON_TERMINAL))
    def test_is_terminal_false(self, status: str) -> None:
        assert is_terminal_receipt_status(status) is False

    def test_unknown_status_not_terminal(self) -> None:
        assert is_terminal_receipt_status("unknown") is False


class TestTerminalOutboxStatus:
    """TERMINAL_OUTBOX_STATUSES and is_terminal_outbox_status."""

    EXPECTED_TERMINAL = {"sent", "dead_lettered", "cancelled", "abandoned"}
    EXPECTED_NON_TERMINAL = {"pending", "in_progress", "queued", "retry_wait"}

    def test_terminal_set(self) -> None:
        assert TERMINAL_OUTBOX_STATUSES == self.EXPECTED_TERMINAL

    @pytest.mark.parametrize("status", sorted(EXPECTED_TERMINAL))
    def test_is_terminal_true(self, status: str) -> None:
        assert is_terminal_outbox_status(status) is True

    @pytest.mark.parametrize("status", sorted(EXPECTED_NON_TERMINAL))
    def test_is_terminal_false(self, status: str) -> None:
        assert is_terminal_outbox_status(status) is False

    def test_unknown_status_not_terminal(self) -> None:
        assert is_terminal_outbox_status("unknown") is False


# ---------------------------------------------------------------------------
# Claimable helper
# ---------------------------------------------------------------------------


class TestClaimableOutboxStatus:
    """CLAIMABLE_OUTBOX_STATUSES and is_claimable_outbox_status."""

    EXPECTED_CLAIMABLE = {"pending", "retry_wait"}

    def test_claimable_set(self) -> None:
        assert CLAIMABLE_OUTBOX_STATUSES == self.EXPECTED_CLAIMABLE

    @pytest.mark.parametrize("status", sorted(EXPECTED_CLAIMABLE))
    def test_is_claimable_true(self, status: str) -> None:
        assert is_claimable_outbox_status(status) is True

    def test_in_progress_not_claimable(self) -> None:
        assert is_claimable_outbox_status("in_progress") is False

    def test_queued_not_claimable(self) -> None:
        assert is_claimable_outbox_status("queued") is False

    def test_terminal_not_claimable(self) -> None:
        for s in TERMINAL_OUTBOX_STATUSES:
            assert is_claimable_outbox_status(s) is False

    def test_unknown_not_claimable(self) -> None:
        assert is_claimable_outbox_status("unknown") is False


# ---------------------------------------------------------------------------
# Accepted outcome helper
# ---------------------------------------------------------------------------


class TestAcceptedOutcomeStatus:
    """ACCEPTED_OUTCOME_STATUSES and is_accepted_outcome_status."""

    EXPECTED_ACCEPTED = {"success", "queued"}
    EXPECTED_NOT_ACCEPTED = {"transient_failure", "permanent_failure", "skipped"}

    def test_accepted_set(self) -> None:
        assert ACCEPTED_OUTCOME_STATUSES == self.EXPECTED_ACCEPTED

    @pytest.mark.parametrize("status", sorted(EXPECTED_ACCEPTED))
    def test_is_accepted_true(self, status: str) -> None:
        assert is_accepted_outcome_status(status) is True

    @pytest.mark.parametrize("status", sorted(EXPECTED_NOT_ACCEPTED))
    def test_is_accepted_false(self, status: str) -> None:
        assert is_accepted_outcome_status(status) is False

    def test_unknown_not_accepted(self) -> None:
        assert is_accepted_outcome_status("unknown") is False


# ---------------------------------------------------------------------------
# Receipt transition validation
# ---------------------------------------------------------------------------


class TestReceiptTransitions:
    """validate_receipt_transition for allowed and invalid transitions."""

    def test_queued_to_sent(self) -> None:
        assert validate_receipt_transition("queued", "sent") is True

    def test_failed_to_dead_lettered(self) -> None:
        assert validate_receipt_transition("failed", "dead_lettered") is True

    def test_sent_has_no_outgoing(self) -> None:
        assert validate_receipt_transition("sent", "queued") is False
        assert validate_receipt_transition("sent", "failed") is False

    def test_dead_lettered_has_no_outgoing(self) -> None:
        assert validate_receipt_transition("dead_lettered", "sent") is False

    def test_suppressed_has_no_outgoing(self) -> None:
        assert validate_receipt_transition("suppressed", "sent") is False

    def test_queued_to_failed_invalid(self) -> None:
        assert validate_receipt_transition("queued", "failed") is False

    def test_failed_to_sent_invalid(self) -> None:
        assert validate_receipt_transition("failed", "sent") is False

    def test_unknown_source_returns_false(self) -> None:
        assert validate_receipt_transition("unknown", "sent") is False

    def test_unknown_target_returns_false(self) -> None:
        assert validate_receipt_transition("queued", "unknown") is False


class TestIsValidQueuedToSentTransition:
    """is_valid_queued_to_sent_transition convenience helper."""

    def test_queued_source(self) -> None:
        assert is_valid_queued_to_sent_transition("queued") is True

    def test_failed_source(self) -> None:
        assert is_valid_queued_to_sent_transition("failed") is False

    def test_sent_source(self) -> None:
        assert is_valid_queued_to_sent_transition("sent") is False


# ---------------------------------------------------------------------------
# Outbox transition validation
# ---------------------------------------------------------------------------


class TestOutboxTransitions:
    """validate_outbox_transition for allowed and invalid transitions."""

    # Lease acquisition paths.
    def test_pending_to_in_progress(self) -> None:
        assert validate_outbox_transition("pending", "in_progress") is True

    def test_retry_wait_to_in_progress(self) -> None:
        assert validate_outbox_transition("retry_wait", "in_progress") is True

    def test_retry_wait_to_cancelled(self) -> None:
        assert validate_outbox_transition("retry_wait", "cancelled") is True

    def test_retry_wait_to_dead_lettered(self) -> None:
        assert validate_outbox_transition("retry_wait", "dead_lettered") is True

    def test_queued_to_in_progress(self) -> None:
        assert validate_outbox_transition("queued", "in_progress") is True

    def test_queued_to_sent(self) -> None:
        assert validate_outbox_transition("queued", "sent") is True

    # Delivery outcome from in_progress.
    @pytest.mark.parametrize(
        "target",
        ["queued", "sent", "retry_wait", "dead_lettered", "cancelled", "abandoned"],
    )
    def test_in_progress_to_targets(self, target: str) -> None:
        assert validate_outbox_transition("in_progress", target) is True

    # Invalid transitions.
    def test_pending_to_sent_invalid(self) -> None:
        assert validate_outbox_transition("pending", "sent") is False

    def test_pending_to_dead_lettered_invalid(self) -> None:
        assert validate_outbox_transition("pending", "dead_lettered") is False

    def test_in_progress_to_pending_invalid(self) -> None:
        assert validate_outbox_transition("in_progress", "pending") is False

    # Terminal states have no outgoing transitions.
    @pytest.mark.parametrize("source", sorted(TERMINAL_OUTBOX_STATUSES))
    def test_terminal_no_outgoing(self, source: str) -> None:
        assert validate_outbox_transition(source, "pending") is False
        assert validate_outbox_transition(source, "in_progress") is False

    def test_unknown_source_returns_false(self) -> None:
        assert validate_outbox_transition("unknown", "sent") is False

    def test_unknown_target_returns_false(self) -> None:
        assert validate_outbox_transition("pending", "unknown") is False


# ---------------------------------------------------------------------------
# Transition table completeness -- every known status is accounted for
# ---------------------------------------------------------------------------


class TestTransitionTableCompleteness:
    """Every known status is either a key in the transition table or a
    terminal status with no outgoing transitions."""

    def test_receipt_table_covers_all_known(self) -> None:
        all_sources = set(RECEIPT_TRANSITIONS.keys())
        terminal_with_no_entry = TERMINAL_RECEIPT_STATUSES - all_sources
        # Every receipt status must be either in the table or terminal.
        assert RECEIPT_STATUSES == all_sources | terminal_with_no_entry

    def test_receipt_table_targets_are_known(self) -> None:
        for targets in RECEIPT_TRANSITIONS.values():
            assert targets <= RECEIPT_STATUSES

    def test_outbox_table_covers_all_known(self) -> None:
        all_sources = set(OUTBOX_TRANSITIONS.keys())
        terminal_with_no_entry = TERMINAL_OUTBOX_STATUSES - all_sources
        assert OUTBOX_STATUSES == all_sources | terminal_with_no_entry

    def test_outbox_table_targets_are_known(self) -> None:
        for targets in OUTBOX_TRANSITIONS.values():
            assert targets <= OUTBOX_STATUSES


# ---------------------------------------------------------------------------
# Cross-vocabulary edge cases
# ---------------------------------------------------------------------------


class TestCrossVocabularyEdgeCases:
    """Statuses that belong to one vocabulary but not another."""

    def test_skipped_not_a_receipt_status(self) -> None:
        """skipped is an outcome status but not a receipt status."""
        assert validate_receipt_status("skipped") is False
        assert validate_outcome_status("skipped") is True

    def test_suppressed_not_a_successful_outcome(self) -> None:
        """suppressed is a receipt status but not an accepted outcome."""
        assert validate_receipt_status("suppressed") is True
        assert is_accepted_outcome_status("suppressed") is False

    def test_failed_not_terminal_receipt(self) -> None:
        """failed is non-terminal because it can lead to dead_lettered."""
        assert is_terminal_receipt_status("failed") is False

    def test_enqueued_not_an_outbox_status(self) -> None:
        assert validate_outbox_status("enqueued") is False
        assert "enqueued" in ADAPTER_DELIVERY_STATUSES
