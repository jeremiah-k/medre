"""Tests for observability/classification.py covering lines 93-94.

Tests the ``outbox_not_owned`` detection in ``infer_failure_kind``.
"""

from __future__ import annotations

from medre.core.observability.classification import infer_failure_kind


class TestInferFailureKindOutboxNotOwned:
    """infer_failure_kind maps outbox_not_owned error patterns correctly."""

    def test_outbox_not_owned_literal(self) -> None:
        assert infer_failure_kind("outbox_not_owned", "failed") == "outbox_not_owned"

    def test_outbox_row_not_owned_phrase(self) -> None:
        assert (
            infer_failure_kind("outbox row not owned: terminal:sent", "failed")
            == "outbox_not_owned"
        )

    def test_outbox_not_owned_case_insensitive(self) -> None:
        """The function lowercases the error, so mixed case still matches."""
        assert (
            infer_failure_kind("Outbox_Not_Owned detected", "failed")
            == "outbox_not_owned"
        )

    def test_outbox_row_not_owned_with_active_reason(self) -> None:
        assert (
            infer_failure_kind(
                "outbox row not owned: active:other_worker:abc", "failed"
            )
            == "outbox_not_owned"
        )
