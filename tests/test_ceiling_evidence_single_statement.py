"""The radio evidence read observes both candidates in one statement.

Reading the retained summary and the live checkpoint in two separate
queries admits a sweep that commits between them: the first read finds no
summary, the second finds no checkpoint, and preserved acceptance
evidence looks missing. Tracing the executed statements pins the
combined single-statement read.
"""

from __future__ import annotations

import pytest

import medre.core.storage.sqlite.storage as storage_module
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _install_elapsed_policy
from tests.helpers.handoff_resolution import (
    _bind_and_progress,
    _seed_reserved_claim,
)


async def test_evidence_read_traces_one_combined_statement(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-single-statement",
        event_id="ceiling-single-statement",
    )
    await _bind_and_progress(temp_storage, provenance, ["accepted"])
    await _install_elapsed_policy(temp_storage, provenance.event_id)

    traced: list[str] = []
    real_read_one = storage_module.sync_read_one

    def traced_read_one(db, lock, sql, params=()):
        traced.append(sql)
        return real_read_one(db, lock, sql, params)

    monkeypatch.setattr(storage_module, "sync_read_one", traced_read_one)

    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.provenance.event_id == provenance.event_id
    assert summary.revision >= 1

    # Both candidates were candidates of exactly one statement: the
    # retained summary arm and the denied live checkpoint arm.
    assert len(traced) == 1
    statement = " ".join(traced[0].split())
    assert "UNION ALL" in statement
    assert "FROM radio_attempt_evidence_summaries" in statement
    assert "FROM radio_attempt_progress" in statement
