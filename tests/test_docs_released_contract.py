"""Keep current operator guidance aligned with released contracts."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_storage_policy_documents_released_catalog_contract() -> None:
    """§10 states exact released-catalog acceptance with refusal otherwise."""
    section = (
        _read("docs/spec/storage.md")
        .split("## 10. Database Compatibility Policy", 1)[1]
        .split("### 10.1", 1)[0]
    )
    assert "accepts the exact released catalog" in section
    assert "forward-only supported predecessor" in section
    assert "No predecessor ships today" in section
    assert "Recognized missing" not in section
    assert "_prerelease_migrate" not in section
    radio = _read("docs/ops/radio-multipart.md")
    assert "only if writable startup rejects" in radio
    assert "needs no reset" in radio


def test_installed_source_and_thread_fallback_contracts() -> None:
    getting_started = _read("docs/ops/getting-started.md")
    assert "Upgrading preserves existing saved" in getting_started
    assert "Git remains necessary" in getting_started
    for path in ("docs/spec/event-model.md", "docs/spec/routing-delivery.md"):
        text = _read(path)
        assert (
            "bound destination thread root" in text
            or "root is bound in the destination room" in text
        )
        assert "independently bound explicit reply parent" in text


def test_packaging_status_is_not_an_operator_banner() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    for path in [ROOT / "README.md", *(ROOT / "docs/ops").rglob("*.md")]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"Development Status :: 3 - Alpha|no stable public API|Maturity note",
            text,
            re.IGNORECASE,
        ), path
