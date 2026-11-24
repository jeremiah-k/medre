"""Guard the content-retention claims against documented source boundaries."""

from pathlib import Path

_DOC = Path(__file__).resolve().parents[1] / "docs/dev/provider-retention-inventory.md"


def test_summary_distinguishes_content_stores_and_credentials() -> None:
    source = _DOC.read_text()
    summary = source.split("## Cross-transport summary", 1)[1].split(
        "## What cannot", 1
    )[0]
    matrix = next(line for line in summary.splitlines() if line.startswith("| Matrix "))
    cells = [x.strip() for x in matrix.strip("|").split("|")]
    assert "credential token sidecar" not in cells[1]
    assert "credential token sidecar" in cells[2]
    assert cells[4].startswith("yes")
    assert "Four transports" in summary
    assert "firmware unknown" in summary


def test_unmeasured_remote_and_device_retention_is_not_reported_as_erased() -> None:
    source = _DOC.read_text()
    assert "firmware message-content retention unestablished" in source
    assert "deletion after fetch is unverified" in source
    assert "deletion after delivery is unverified" in source
    assert "**only the bytes it actually captures**" in source
