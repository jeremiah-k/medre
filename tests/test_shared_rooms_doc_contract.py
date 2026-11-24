"""Shared-room deployment guidance cannot promise distributed dedup or E2EE."""

from pathlib import Path

_DOC = Path(__file__).resolve().parents[1] / "docs/ops/shared-rooms-with-mmrelay.md"


def test_shared_room_boundary_is_explicit() -> None:
    source = _DOC.read_text()
    assert "one bridge per radio mesh" in source
    assert "does **not** prove exactly-once radio delivery" in source
    assert "MEDRE has no peer-relay trust list" in source
    assert "selecting\nthe setting does not prove either bridge can decrypt" in source
    assert "[sender/origin]: [node/mesh]:" in source
