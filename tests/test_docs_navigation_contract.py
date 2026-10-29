"""Navigation and onboarding contracts for the combined Git-first workflow."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)(?:\s+#+)?\s*$")


def _anchors(path: Path) -> set[str]:
    """Approximate GitHub heading slugs, including repeated headings."""
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _HEADING.match(line)
        if match is None:
            continue
        value = re.sub(r"<[^>]+>", "", match.group(1)).lower().strip()
        value = value.replace("`", "").replace("*", "").replace("~", "")
        value = re.sub(r"[^\w\- ]", "", value).replace(" ", "-")
        count = counts.get(value, 0)
        counts[value] = count + 1
        anchors.add(value if count == 0 else f"{value}-{count}")
    return anchors


@pytest.mark.parametrize(
    "source",
    [_ROOT / "README.md", *sorted(_DOCS.rglob("*.md"))],
    ids=lambda p: str(p.relative_to(_ROOT)),
)
def test_local_document_heading_links_resolve(source: Path) -> None:
    """Prevent renamed guide sections from leaving apparently valid dead links."""
    failures: list[str] = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        for match in _LINK.finditer(line):
            target = match.group(1)
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or "#" not in target:
                continue
            filename, anchor = target.split("#", 1)
            destination = (source.parent / filename).resolve() if filename else source
            if destination.is_file() and destination.suffix == ".md":
                if anchor and anchor not in _anchors(destination):
                    failures.append(f"{number}: {target}")
    assert not failures, f"{source.relative_to(_ROOT)}: broken headings: {failures}"


def test_onboarding_has_distinct_tool_and_project_workflows() -> None:
    guide = (_DOCS / "ops/getting-started.md").read_text(encoding="utf-8")
    assert "uv tool install" in guide
    assert "medre adapters install matrix meshtastic" in guide
    deployment = (_DOCS / "ops/install.md").read_text(encoding="utf-8")
    assert "install.md#locked-deployment" in guide
    assert "uv init --bare" in deployment
    assert "uv sync --locked" in deployment
    assert "uv run --locked medre version" in deployment
    assert "not production-ready" in guide
    assert "WAL sidecars" in deployment
    assert "## 1. Install" in guide
    assert "## 2. Verify" in guide
    assert "## 3. Connect" in guide
    assert "## 4. Choose" in guide
    assert "## 5. Run" in guide


def test_backup_guide_avoids_single_file_sqlite_backup_instructions() -> None:
    guide = (_DOCS / "ops/operator-workflows.md").read_text(encoding="utf-8")
    section = guide.split("### Step-by-step reset procedure", 1)[1].split(
        "### What happens to the old data", 1
    )[0]
    assert "--backup --yes" in section
    assert "not** a complete" in section
    assert "another process is writing" in section
    assert "cp ~/.local/state/medre" not in section
    assert "rm ~/.local/state/medre" not in section


def test_multi_source_smoke_examples_use_instance_ids() -> None:
    guide = (_DOCS / "dev/testing.md").read_text(encoding="utf-8")
    section = guide.split("## Multi-source fake smoke coverage", 1)[1].split(
        "## Synthetic lifecycle", 1
    )[0]
    assert "--source-adapter matrix_1" in section
    assert "--source-adapter meshtastic_1" in section


def test_beginner_guides_do_not_reverse_one_way_routes_or_leak_configs() -> None:
    """Operator examples must respect direction and exclude credential files."""
    start = (_DOCS / "ops/getting-started.md").read_text(encoding="utf-8")
    install = (_DOCS / "ops/install.md").read_text(encoding="utf-8")
    presentation = (_DOCS / "ops/message-presentation.md").read_text(encoding="utf-8")
    assert "A one-way route must not forward messages in reverse" in start
    assert "test the reverse" in start
    for filename in ("matrix.yaml", "connections.yaml", "bridge.yaml"):
        assert f"/{filename}" in install.split("cat >> .gitignore", 1)[1]
    assert "does not display the rendered destination message" in " ".join(
        presentation.split()
    )
