"""Tests for the pure outbox shutdown policy classifier.

Covers :class:`OutboxShutdownClassification` and
:func:`classify_outbox_shutdown_policy` from
:mod:`medre.core.evidence.shutdown`.

Validates:
* All 8 ``OUTBOX_STATUSES`` produce correct classifications.
* Resumable statuses have ``resume_on_restart=True``.
* Terminal statuses have ``resume_on_restart=False``.
* No classification requests ``mutate_outbox`` or ``append_receipt``.
* ``to_dict()`` produces JSON-safe values with sorted keys.
* Unknown status raises ``ValueError``.
* Dataclass is frozen (immutable).
"""

from __future__ import annotations

import dataclasses

import pytest

from medre.core.evidence.shutdown import (
    OutboxShutdownClassification,
    classify_outbox_shutdown_policy,
)

# ---------------------------------------------------------------------------
# Expected mapping: status -> (classification, resume_on_restart)
# ---------------------------------------------------------------------------

_RESUMABLE_EXPECTED: dict[str, str] = {
    "pending": "resumable_pending",
    "retry_wait": "resumable_retry_wait",
    "in_progress": "resumable_in_progress",
    "queued": "resumable_queued",
}

_TERMINAL_EXPECTED: dict[str, str] = {
    "sent": "terminal_sent",
    "dead_lettered": "terminal_dead_lettered",
    "cancelled": "terminal_cancelled",
    "abandoned": "terminal_abandoned",
}

_ALL_EXPECTED: dict[str, tuple[str, bool]] = {
    **{s: (c, True) for s, c in _RESUMABLE_EXPECTED.items()},
    **{s: (c, False) for s, c in _TERMINAL_EXPECTED.items()},
}

_ALL_PARAMETRIZED = [
    (status, cls, resume) for status, (cls, resume) in _ALL_EXPECTED.items()
]


# ---------------------------------------------------------------------------
# Parametrized: all outbox statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_classification", "expected_resume"),
    _ALL_PARAMETRIZED,
    ids=list(_ALL_EXPECTED.keys()),
)
def test_classify_outbox_status(
    status: str,
    expected_classification: str,
    expected_resume: bool,
) -> None:
    """Every known outbox status produces the correct classification."""
    result = classify_outbox_shutdown_policy(status)
    assert isinstance(result, OutboxShutdownClassification)
    assert result.status == status
    assert result.classification == expected_classification
    assert result.resume_on_restart is expected_resume


# ---------------------------------------------------------------------------
# No mutation / no receipt append — all statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    list(_ALL_EXPECTED),
    ids=list(_ALL_EXPECTED),
)
def test_no_mutation_or_receipt(status: str) -> None:
    """Graceful shutdown never requests outbox mutation or receipt append."""
    result = classify_outbox_shutdown_policy(status)
    assert result.mutate_outbox is False
    assert result.append_receipt is False


# ---------------------------------------------------------------------------
# Resumable statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", list(_RESUMABLE_EXPECTED), ids=list(_RESUMABLE_EXPECTED)
)
def test_resumable_resume_on_restart(status: str) -> None:
    """Resumable statuses have resume_on_restart=True."""
    result = classify_outbox_shutdown_policy(status)
    assert result.resume_on_restart is True
    assert result.classification.startswith("resumable_")


# ---------------------------------------------------------------------------
# Terminal statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", list(_TERMINAL_EXPECTED), ids=list(_TERMINAL_EXPECTED)
)
def test_terminal_no_resume(status: str) -> None:
    """Terminal statuses have resume_on_restart=False."""
    result = classify_outbox_shutdown_policy(status)
    assert result.resume_on_restart is False
    assert result.classification.startswith("terminal_")


# ---------------------------------------------------------------------------
# Evidence reason is non-empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(_ALL_EXPECTED), ids=list(_ALL_EXPECTED))
def test_evidence_reason_non_empty(status: str) -> None:
    """Every classification has a non-empty evidence_reason."""
    result = classify_outbox_shutdown_policy(status)
    assert isinstance(result.evidence_reason, str)
    assert len(result.evidence_reason) > 0


# ---------------------------------------------------------------------------
# to_dict() is JSON-safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(_ALL_EXPECTED), ids=list(_ALL_EXPECTED))
def test_to_dict_json_safe(status: str) -> None:
    """to_dict() returns a dict with sorted keys and JSON-safe values."""
    result = classify_outbox_shutdown_policy(status)
    d = result.to_dict()
    assert isinstance(d, dict)
    # Keys are sorted.
    assert list(d.keys()) == sorted(d.keys())
    # All values are JSON-safe types.
    for value in d.values():
        assert isinstance(value, (str, bool, int, float, type(None)))
    # Contains expected fields.
    expected_keys = {
        "status",
        "classification",
        "mutate_outbox",
        "append_receipt",
        "resume_on_restart",
        "evidence_reason",
    }
    assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Unknown status raises ValueError
# ---------------------------------------------------------------------------


def test_unknown_status_raises_value_error() -> None:
    """Unknown status string raises ValueError."""
    with pytest.raises(ValueError, match="Unknown outbox status"):
        classify_outbox_shutdown_policy("definitely_not_real")


@pytest.mark.parametrize(
    "bad_status",
    ["unknown", "processing", "delivered", "FAILED", "Sent", ""],
    ids=["unknown", "processing", "delivered", "FAILED_upper", "Sent_mixed", "empty"],
)
def test_various_invalid_statuses(bad_status: str) -> None:
    """Various invalid status strings raise ValueError."""
    with pytest.raises(ValueError):
        classify_outbox_shutdown_policy(bad_status)


# ---------------------------------------------------------------------------
# Frozen dataclass
# ---------------------------------------------------------------------------


def test_frozen_dataclass() -> None:
    """OutboxShutdownClassification is immutable."""
    result = classify_outbox_shutdown_policy("pending")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.mutate_outbox = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Exact coverage: all OUTBOX_STATUSES from delivery_state.py
# ---------------------------------------------------------------------------


def test_all_outbox_statuses_covered() -> None:
    """All 8 OUTBOX_STATUSES are classified without error."""
    from medre.core.engine.pipeline.delivery_state import OUTBOX_STATUSES

    for status in OUTBOX_STATUSES:
        result = classify_outbox_shutdown_policy(status)
        assert result.status == status
        assert result.classification is not None


def test_resumable_matches_non_terminal() -> None:
    """Resumable classifications exactly match non-terminal outbox statuses."""
    from medre.core.engine.pipeline.delivery_state import (
        OUTBOX_STATUSES,
        TERMINAL_OUTBOX_STATUSES,
    )

    non_terminal = OUTBOX_STATUSES - TERMINAL_OUTBOX_STATUSES
    for status in non_terminal:
        result = classify_outbox_shutdown_policy(status)
        assert result.resume_on_restart is True, f"{status} should be resumable"

    for status in TERMINAL_OUTBOX_STATUSES:
        result = classify_outbox_shutdown_policy(status)
        assert result.resume_on_restart is False, f"{status} should be terminal"
