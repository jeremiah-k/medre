"""Chunked diagnostic resolution binds the right identities, in order.

Three calls with 802 bound parameters can pass a count-only check while
the typed-fact arm binds a wrong or reordered outbox identity chunk. The
fake here records the full chunks, asserts both outbox arms repeat the
same identities in the same order, and returns a diagnostic per identity
so the resolved fields must be filled on both sides of every chunk
boundary.
"""

from __future__ import annotations

from medre.core.storage.sqlite._content_identity import (
    CEILING_CLOCK_AT_EXECUTION,
)
from medre.core.storage.sqlite._evidence_diagnostics import (
    _EvidenceDiagnosticsReadMixin,
)


class _RecordingFake(_EvidenceDiagnosticsReadMixin):
    def __init__(self, family: str, identities: list[str]) -> None:
        self.family = family
        self.identities = identities
        self.calls: list[int] = []
        self.chunks: list[list[str]] = []

    async def _read_all(self, sql, params=()):  # type: ignore[no-untyped-def]
        self.calls.append(len(params))
        assert params[0] is CEILING_CLOCK_AT_EXECUTION
        assert params[1] == self.family
        chunk = list(params[2 : 2 + 400])
        self.chunks.append(chunk)
        if self.family == "outbox":
            # The typed-fact UNION arm repeats the chunk in the same order.
            assert list(params[402:]) == chunk
        page = len(self.chunks) - 1
        assert chunk == self.identities[page * 400 : (page + 1) * 400]
        rows = []
        for row_id in chunk:
            rows.append(
                {
                    "diag_id": int(row_id.split("-")[1]) + 1,
                    "row_id": row_id,
                    "field": (
                        "failure_kind_detail" if self.family == "outbox" else "error"
                    ),
                    "attempt": 1,
                    "diagnostic": row_id,
                }
            )
        return rows


def _identities(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-{i}" for i in range(count)]


async def test_receipt_chunks_bind_identities_in_order_and_fill_every_row():
    identities = _identities("receipt", 1200)
    rows = [
        {
            "receipt_id": row_id,
            "error": None,
            "capability_reason": None,
            "rendering_evidence": None,
        }
        for row_id in identities
    ]
    fake = _RecordingFake("receipt", identities)
    await fake._resolve_evidence_diagnostics(
        "receipt", rows, identity_column="receipt_id"
    )
    assert fake.calls == [402, 402, 402]
    assert [chunk for chunk in fake.chunks] == [
        identities[0:400],
        identities[400:800],
        identities[800:1200],
    ]
    assert [row["error"] for row in rows] == identities
    # Both sides of the first chunk boundary were filled from their own
    # chunk's identities.
    assert rows[399]["error"] == "receipt-399"
    assert rows[400]["error"] == "receipt-400"


async def test_outbox_chunks_bind_identities_twice_in_order_and_fill_fields():
    identities = _identities("obox", 1200)
    rows = [
        {
            "outbox_id": row_id,
            "status": "retry_wait",
            "attempt_number": 1,
            "failure_kind_detail": None,
            "error_summary": None,
        }
        for row_id in identities
    ]
    fake = _RecordingFake("outbox", identities)
    await fake._resolve_evidence_diagnostics(
        "outbox", rows, identity_column="outbox_id"
    )
    assert fake.calls == [802, 802, 802]
    assert max(fake.calls) <= 999
    assert [chunk for chunk in fake.chunks] == [
        identities[0:400],
        identities[400:800],
        identities[800:1200],
    ]
    assert [row["failure_kind_detail"] for row in rows] == identities
    assert all(row["error_summary"] is None for row in rows)
    # The chunk boundary did not reorder or drop the typed facts.
    assert rows[399]["failure_kind_detail"] == "obox-399"
    assert rows[400]["failure_kind_detail"] == "obox-400"
