"""Keep current operator guidance aligned with released contracts."""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_storage_policy_documents_exact_missing_object_allowlist() -> None:
    tree = ast.parse(_read("src/medre/core/storage/sqlite/schema_validation.py"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_ADDITIVE_OBJECTS"
    )
    assert isinstance(assignment.value, ast.Call)
    objects = ast.literal_eval(assignment.value.args[0])
    section = (
        _read("docs/spec/storage.md")
        .split("## 10. Database Compatibility Policy", 1)[1]
        .split("### 10.1", 1)[0]
    )
    for _, name in objects:
        assert f"`{name}`" in section
    for required in {
        "radio_attempt_progress",
        "attachment_blobs",
        "event_attachment_associations",
    }:
        assert ("table", required) not in objects
    radio = _read("docs/ops/radio-multipart.md")
    assert "only if writable startup rejects" in radio
    assert "needs no reset" in radio


def test_installed_source_and_thread_fallback_contracts() -> None:
    getting_started = _read("docs/ops/getting-started.md")
    assert "Upgrading preserves saved" in getting_started
    assert "Git-backed requests" in getting_started
    for path in ("docs/spec/event-model.md", "docs/spec/routing-delivery.md"):
        text = _read(path)
        assert (
            "bound destination thread root" in text
            or "root is bound in the destination room" in text
        )
        assert "independently bound explicit reply parent" in text


def test_packaging_status_is_not_an_operator_banner() -> None:
    for path in [ROOT / "README.md", *(ROOT / "docs/ops").rglob("*.md")]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"Development Status :: 3 - Alpha|no stable public API|Maturity note",
            text,
            re.IGNORECASE,
        ), path


def test_collection_policy_defers_to_the_storage_allowlist() -> None:
    section = _read("docs/spec/content-collection-guard.md")
    assert "storage.md#10-database-compatibility-policy" in section
    assert "Unsupported catalogs MUST be refused before schema DDL" in " ".join(
        section.split()
    )
    assert "collection policy introduces no additional migration authority" in section


def test_evidence_collection_does_not_advertise_a_removed_cli_flag() -> None:
    tree = ast.parse(_read("src/medre/runtime/evidence/_helpers.py"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_LIMITATIONS"
    )
    assert assignment.value is not None
    limitations = ast.literal_eval(assignment.value)
    assert all("--include-refresh-health" not in item for item in limitations)
    example = json.loads(_read("docs/schemas/examples/evidence-bundle-example.json"))
    assert example["limitations"] == limitations
