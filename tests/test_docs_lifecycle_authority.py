"""Lifecycle authority enforcement tests.

Cheap guardrails verifying lifecycle authority docs/code alignment and
adapter metadata naming conventions.  These tests catch drift between
spec documents and the authoritative status vocabularies in
``delivery_state.py``, and prevent ``delivery_status`` from leaking
into adapter metadata dicts.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from medre.core.engine.pipeline.delivery_state import (
    ADAPTER_DELIVERY_STATUSES,
    OUTBOX_STATUSES,
    OUTCOME_STATUSES,
    RECEIPT_STATUSES,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent

SPEC_DIR = _ROOT / "docs" / "spec"
DEV_DIR = _ROOT / "docs" / "dev"
ADAPTERS_DIR = _ROOT / "src" / "medre" / "adapters"

DELIVERY_LIFECYCLE_MD = SPEC_DIR / "delivery-lifecycle.md"
LIFECYCLE_AUDIT_MD = DEV_DIR / "lifecycle-authority-audit.md"
STATE_MACHINES_MD = SPEC_DIR / "state-machines.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read a file, raising a clear error if missing."""
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path.relative_to(_ROOT)}")
    return path.read_text(encoding="utf-8")


def _extract_backtick_statuses(text: str) -> set[str]:
    """Extract all backtick-wrapped tokens that look like status values.

    Matches ``word_like_tokens`` (lowercase, underscores, digits) inside
    backticks.  Filters out obvious non-status patterns like constant
    names (ALL_CAPS) and file paths (containing slashes or dots).
    """
    raw = set(re.findall(r"`([a-z][a-z0-9_]*)`", text))
    # Exclude constant-like names (ALL CAPS with underscores)
    return {s for s in raw if not re.match(r"^[A-Z][A-Z0-9_]+$", s)}


def _adapter_py_files() -> list[Path]:
    """Return all ``.py`` files under ``src/medre/adapters/``."""
    return sorted(ADAPTERS_DIR.rglob("*.py"))


def _parse_adapter_results(
    source: str, filename: str
) -> list[tuple[str | None, dict[str, str]]]:
    """Parse ``AdapterDeliveryResult(...)`` calls from *source*.

    Returns a list of ``(delivery_status_value, metadata_keys)`` tuples.
    Only literal keyword arguments are captured; dynamic values are
    recorded as ``None``.

    *delivery_status_value* is the string literal passed as the
    ``delivery_status`` keyword, or ``None`` if absent / non-literal.

    *metadata_keys* maps each key found in the ``metadata=MappingProxyType({...})``
    argument to its string value (or ``"<non-literal>"`` if not a string
    literal).
    """
    tree = ast.parse(source, filename=filename)
    results: list[tuple[str | None, dict[str, str]]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Match AdapterDeliveryResult(...) calls
        func = node.func
        is_target = False
        if isinstance(func, ast.Name) and func.id == "AdapterDeliveryResult":
            is_target = True
        elif isinstance(func, ast.Attribute) and func.attr == "AdapterDeliveryResult":
            is_target = True

        if not is_target:
            continue

        ds_value: str | None = None
        meta_keys: dict[str, str] = {}

        for kw in node.keywords:
            # Extract delivery_status keyword
            if kw.arg == "delivery_status" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    ds_value = kw.value.value

            # Extract metadata=MappingProxyType({...})
            if kw.arg == "metadata" and isinstance(kw.value, ast.Call):
                meta_call = kw.value
                meta_func = meta_call.func
                is_mapping_proxy = False
                if (
                    isinstance(meta_func, ast.Name)
                    and meta_func.id == "MappingProxyType"
                ):
                    is_mapping_proxy = True
                elif (
                    isinstance(meta_func, ast.Attribute)
                    and meta_func.attr == "MappingProxyType"
                ):
                    is_mapping_proxy = True

                if (
                    is_mapping_proxy
                    and meta_call.args
                    and isinstance(meta_call.args[0], ast.Dict)
                ):
                    d = meta_call.args[0]
                    for k, v in zip(d.keys, d.values, strict=False):
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            key_str = k.value
                            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                meta_keys[key_str] = v.value
                            else:
                                meta_keys[key_str] = "<non-literal>"

        results.append((ds_value, meta_keys))

    return results


# ===========================================================================
# 1. Document existence
# ===========================================================================


class TestDocumentExistence:
    """Required lifecycle authority documents must exist."""

    def test_delivery_lifecycle_md_exists(self) -> None:
        """docs/spec/delivery-lifecycle.md must exist."""
        assert (
            DELIVERY_LIFECYCLE_MD.is_file()
        ), f"Missing: {DELIVERY_LIFECYCLE_MD.relative_to(_ROOT)}"

    def test_lifecycle_authority_audit_md_exists(self) -> None:
        """docs/dev/lifecycle-authority-audit.md must exist."""
        assert (
            LIFECYCLE_AUDIT_MD.is_file()
        ), f"Missing: {LIFECYCLE_AUDIT_MD.relative_to(_ROOT)}"


# ===========================================================================
# 2. Spec ↔ code vocabulary alignment
# ===========================================================================


class TestReceiptStatusAlignment:
    """Receipt statuses in spec docs must align with ``RECEIPT_STATUSES``."""

    @pytest.fixture(scope="class")
    def spec_receipt_statuses(self) -> set[str]:
        """Receipt statuses mentioned in spec documents."""
        statuses: set[str] = set()
        for path in (DELIVERY_LIFECYCLE_MD, STATE_MACHINES_MD):
            if path.exists():
                statuses |= _extract_backtick_statuses(path.read_text("utf-8"))
        # Filter to only known receipt-status-like values from the spec.
        # The receipt statuses are a closed set; we intersect with known
        # code values and also check for specific individual statuses.
        return statuses

    def test_all_code_receipt_statuses_in_spec(self) -> None:
        """Every status in RECEIPT_STATUSES must appear in the spec docs."""
        content = _read(DELIVERY_LIFECYCLE_MD) + "\n" + _read(STATE_MACHINES_MD)
        _extract_backtick_statuses(content)
        for status in RECEIPT_STATUSES:
            assert f"`{status}`" in content, (
                f"Receipt status '{status}' from RECEIPT_STATUSES "
                f"not found in spec documents"
            )

    def test_receipt_status_table_mentions_all(self) -> None:
        """delivery-lifecycle.md §2.1 must list every RECEIPT_STATUSES value."""
        content = _read(DELIVERY_LIFECYCLE_MD)
        # Find the authoritative vocabularies table row for receipt statuses
        for status in RECEIPT_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"delivery-lifecycle.md does not mention receipt status '{status}'"


class TestOutboxStatusAlignment:
    """Outbox statuses in spec docs must align with ``OUTBOX_STATUSES``."""

    def test_all_code_outbox_statuses_in_spec(self) -> None:
        """Every status in OUTBOX_STATUSES must appear in the spec docs."""
        content = _read(DELIVERY_LIFECYCLE_MD) + "\n" + _read(STATE_MACHINES_MD)
        for status in OUTBOX_STATUSES:
            assert f"`{status}`" in content, (
                f"Outbox status '{status}' from OUTBOX_STATUSES "
                f"not found in spec documents"
            )

    def test_outbox_status_table_mentions_all(self) -> None:
        """delivery-lifecycle.md §2.1 must list every OUTBOX_STATUSES value."""
        content = _read(DELIVERY_LIFECYCLE_MD)
        for status in OUTBOX_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"delivery-lifecycle.md does not mention outbox status '{status}'"


class TestOutcomeStatusAlignment:
    """Outcome statuses in spec docs must align with ``OUTCOME_STATUSES``."""

    def test_all_code_outcome_statuses_in_spec(self) -> None:
        """Every status in OUTCOME_STATUSES must appear in the spec docs."""
        content = _read(DELIVERY_LIFECYCLE_MD) + "\n" + _read(STATE_MACHINES_MD)
        for status in OUTCOME_STATUSES:
            assert f"`{status}`" in content, (
                f"Outcome status '{status}' from OUTCOME_STATUSES "
                f"not found in spec documents"
            )


class TestAdapterDeliveryStatusAlignment:
    """Adapter delivery statuses in spec docs must align with
    ``ADAPTER_DELIVERY_STATUSES``."""

    def test_all_adapter_delivery_statuses_in_audit_doc(self) -> None:
        """Every status in ADAPTER_DELIVERY_STATUSES must appear in the
        lifecycle-authority-audit.md vocabulary table."""
        content = _read(LIFECYCLE_AUDIT_MD)
        for status in ADAPTER_DELIVERY_STATUSES:
            assert f"`{status}`" in content, (
                f"Adapter delivery status '{status}' from "
                f"ADAPTER_DELIVERY_STATUSES not found in "
                f"lifecycle-authority-audit.md"
            )


# ===========================================================================
# 3. Adapter delivery_status literal scan
# ===========================================================================


class TestAdapterDeliveryStatusLiterals:
    """Adapter source ``AdapterDeliveryResult(delivery_status=...)`` values
    must belong to ``ADAPTER_DELIVERY_STATUSES``."""

    @pytest.fixture(scope="class")
    def scanned_literals(self) -> list[tuple[str, str | None]]:
        """Scan adapter source for delivery_status literals.

        Returns list of ``(file_path, literal_value)`` tuples.
        """
        found: list[tuple[str, str | None]] = []
        for py_file in _adapter_py_files():
            source = py_file.read_text("utf-8")
            results = _parse_adapter_results(source, str(py_file))
            for ds_value, _meta in results:
                found.append((str(py_file.relative_to(_ROOT)), ds_value))
        return found

    def test_adapter_delivery_status_values_are_known(
        self, scanned_literals: list[tuple[str, str | None]]
    ) -> None:
        """Every explicit delivery_status literal in adapter source must
        be in ADAPTER_DELIVERY_STATUSES."""
        unknown: list[str] = []
        for filepath, ds_value in scanned_literals:
            if ds_value is not None and ds_value not in ADAPTER_DELIVERY_STATUSES:
                unknown.append(f"{filepath}: delivery_status={ds_value!r}")

        assert not unknown, (
            "Unknown adapter delivery_status values found:\n"
            + "\n".join(f"  {u}" for u in unknown)
            + f"\nAllowed: {sorted(ADAPTER_DELIVERY_STATUSES)}"
        )


# ===========================================================================
# 4. Adapter metadata key naming
# ===========================================================================


class TestAdapterMetadataNaming:
    """Adapter metadata dicts must not use ``delivery_status`` as a key.

    The ``delivery_status`` keyword is reserved for
    ``AdapterDeliveryResult.delivery_status`` (the top-level field).
    Adapter-level status evidence in metadata must use ``adapter_status``
    or ``adapter_*`` names to avoid namespace collision with pipeline-level
    receipt / outbox status.
    """

    @pytest.fixture(scope="class")
    def metadata_violations(self) -> list[str]:
        """Scan adapter source for metadata dicts with ``delivery_status`` key."""
        violations: list[str] = []
        for py_file in _adapter_py_files():
            source = py_file.read_text("utf-8")
            results = _parse_adapter_results(source, str(py_file))
            for _ds_value, meta_keys in results:
                if "delivery_status" in meta_keys:
                    violations.append(
                        f"{py_file.relative_to(_ROOT)}: metadata contains "
                        f"key 'delivery_status' — use 'adapter_status' instead"
                    )
        return violations

    def test_no_delivery_status_in_metadata(
        self, metadata_violations: list[str]
    ) -> None:
        """Adapter metadata dicts must not contain ``delivery_status`` key."""
        assert (
            not metadata_violations
        ), "Adapter metadata 'delivery_status' key violations:\n" + "\n".join(
            f"  {v}" for v in metadata_violations
        )


class TestTestMockMetadataNaming:
    """Test mocks constructing ``AdapterDeliveryResult`` must not use
    ``delivery_status`` as a metadata key — same rule as adapters."""

    @pytest.fixture(scope="class")
    def test_mock_violations(self) -> list[str]:
        """Scan test source for AdapterDeliveryResult metadata dicts with
        ``delivery_status`` key."""
        test_dir = _ROOT / "tests"
        violations: list[str] = []
        for py_file in sorted(test_dir.rglob("*.py")):
            source = py_file.read_text("utf-8")
            results = _parse_adapter_results(source, str(py_file))
            for _ds_value, meta_keys in results:
                if "delivery_status" in meta_keys:
                    violations.append(
                        f"{py_file.relative_to(_ROOT)}: metadata contains "
                        f"key 'delivery_status' — use 'adapter_status' instead"
                    )
        return violations

    def test_no_delivery_status_in_test_mocks(
        self, test_mock_violations: list[str]
    ) -> None:
        """Test mock metadata dicts must not contain ``delivery_status`` key."""
        assert (
            not test_mock_violations
        ), "Test mock metadata 'delivery_status' key violations:\n" + "\n".join(
            f"  {v}" for v in test_mock_violations
        )


# ===========================================================================
# 5. MeshCore metadata regression
# ===========================================================================


class TestMeshCoreMetadataRegression:
    """MeshCore adapter metadata must use ``adapter_status``, not
    ``delivery_status``."""

    def test_meshcore_uses_adapter_status(self) -> None:
        """MeshCore real adapter metadata uses ``adapter_status`` key."""
        meshcore_path = ADAPTERS_DIR / "meshcore" / "adapter.py"
        if not meshcore_path.exists():
            pytest.skip("MeshCore adapter not found")

        source = meshcore_path.read_text("utf-8")
        results = _parse_adapter_results(source, str(meshcore_path))

        # Must have at least one AdapterDeliveryResult with metadata
        adapter_status_found = False
        for _ds_value, meta_keys in results:
            if "adapter_status" in meta_keys:
                adapter_status_found = True
            # Should never have delivery_status in metadata
            assert "delivery_status" not in meta_keys, (
                "MeshCore adapter metadata uses 'delivery_status' key "
                "instead of 'adapter_status'"
            )

        assert adapter_status_found, (
            "MeshCore adapter does not use 'adapter_status' in any "
            "AdapterDeliveryResult metadata"
        )

    def test_meshcore_fake_uses_adapter_status(self) -> None:
        """MeshCore fake adapter metadata uses ``adapter_status`` key."""
        fake_path = ADAPTERS_DIR / "fakes" / "meshcore.py"
        if not fake_path.exists():
            pytest.skip("MeshCore fake adapter not found")

        source = fake_path.read_text("utf-8")
        results = _parse_adapter_results(source, str(fake_path))

        adapter_status_found = False
        for _ds_value, meta_keys in results:
            if "adapter_status" in meta_keys:
                adapter_status_found = True
            assert "delivery_status" not in meta_keys, (
                "MeshCore fake adapter metadata uses 'delivery_status' "
                "instead of 'adapter_status'"
            )

        assert adapter_status_found, (
            "MeshCore fake adapter does not use 'adapter_status' in any "
            "AdapterDeliveryResult metadata"
        )
