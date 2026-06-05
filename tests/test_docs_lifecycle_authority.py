"""Lifecycle authority enforcement tests.

Cheap guardrails verifying lifecycle authority docs/code alignment and
adapter metadata naming conventions.  These tests catch drift between
spec documents and the authoritative status vocabularies in
``delivery_state.py``, and prevent ``delivery_status`` from leaking
into adapter metadata dicts.

Wave E additions:
- Classification subset alignment (NON_TERMINAL constants in spec docs).
- Transition table alignment (receipt and outbox transitions in spec docs
  match RECEIPT_TRANSITIONS / OUTBOX_TRANSITIONS).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from medre.core.engine.pipeline.delivery_state import (
    ACCEPTED_OUTCOME_STATUSES,
    ADAPTER_DELIVERY_STATUSES,
    CLAIMABLE_OUTBOX_STATUSES,
    NON_TERMINAL_OUTBOX_STATUSES,
    NON_TERMINAL_RECEIPT_STATUSES,
    OUTBOX_STATUSES,
    OUTBOX_TRANSITIONS,
    OUTCOME_STATUSES,
    RECEIPT_STATUSES,
    RECEIPT_TRANSITIONS,
    TERMINAL_OUTBOX_STATUSES,
    TERMINAL_RECEIPT_STATUSES,
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
    """Extract all backtick-wrapped tokens matching ``[a-z][a-z0-9_]*``."""
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", text))


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

            # Extract metadata={...} (literal dict) or metadata=MappingProxyType({...})
            if kw.arg == "metadata" and isinstance(kw.value, ast.Dict):
                # Literal dict: metadata={...}
                d = kw.value
                for k, v in zip(d.keys, d.values, strict=True):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        key_str = k.value
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            meta_keys[key_str] = v.value
                        else:
                            meta_keys[key_str] = "<non-literal>"
            elif kw.arg == "metadata" and isinstance(kw.value, ast.Call):
                # MappingProxyType({...})
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
                    for k, v in zip(d.keys, d.values, strict=True):
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
# 5. Ambiguous top-level metadata keys (status / state)
# ===========================================================================


#: Bare keys that are ambiguous at the top level of adapter metadata.
#: ``delivery_status`` is already covered by TestAdapterMetadataNaming and
#: TestTestMockMetadataNaming above.  This set catches the remaining
#: ambiguous bare names that could collide with pipeline-level concepts.
_AMBIGUOUS_METADATA_KEYS: frozenset[str] = frozenset({"status", "state"})


class TestAmbiguousTopLevelMetadataKeys:
    """Adapter metadata dicts must not use bare ``status`` or ``state``
    as top-level keys.

    These ambiguous names could be confused with pipeline-level receipt
    or outbox statuses.  Adapters should use namespace-prefixed names
    (e.g. ``adapter_status``, ``meshtastic_channel_index``) or nest
    protocol-specific state under a protocol namespace key (e.g.
    ``metadata["lxmf"]["delivery_state"]``).
    """

    @pytest.fixture(scope="class")
    def adapter_ambiguous_violations(self) -> list[str]:
        """Scan adapter source for bare status/state metadata keys."""
        violations: list[str] = []
        for py_file in _adapter_py_files():
            source = py_file.read_text("utf-8")
            results = _parse_adapter_results(source, str(py_file))
            for _ds_value, meta_keys in results:
                for key in _AMBIGUOUS_METADATA_KEYS:
                    if key in meta_keys:
                        violations.append(
                            f"{py_file.relative_to(_ROOT)}: metadata contains "
                            f"ambiguous top-level key {key!r} — use a "
                            f"namespace-prefixed name (e.g. 'adapter_status')"
                        )
        return violations

    def test_adapter_metadata_no_ambiguous_keys(
        self, adapter_ambiguous_violations: list[str]
    ) -> None:
        """Adapter metadata must not contain bare ``status`` or ``state``."""
        assert (
            not adapter_ambiguous_violations
        ), "Adapter metadata ambiguous key violations:\n" + "\n".join(
            f"  {v}" for v in adapter_ambiguous_violations
        )

    @pytest.fixture(scope="class")
    def test_mock_ambiguous_violations(self) -> list[str]:
        """Scan test source for bare status/state metadata keys in mocks."""
        test_dir = _ROOT / "tests"
        violations: list[str] = []
        for py_file in sorted(test_dir.rglob("*.py")):
            source = py_file.read_text("utf-8")
            results = _parse_adapter_results(source, str(py_file))
            for _ds_value, meta_keys in results:
                for key in _AMBIGUOUS_METADATA_KEYS:
                    if key in meta_keys:
                        violations.append(
                            f"{py_file.relative_to(_ROOT)}: metadata contains "
                            f"ambiguous top-level key {key!r} — use a "
                            f"namespace-prefixed name (e.g. 'adapter_status')"
                        )
        return violations

    def test_test_mock_metadata_no_ambiguous_keys(
        self, test_mock_ambiguous_violations: list[str]
    ) -> None:
        """Test mock metadata must not contain bare ``status`` or ``state``."""
        assert (
            not test_mock_ambiguous_violations
        ), "Test mock metadata ambiguous key violations:\n" + "\n".join(
            f"  {v}" for v in test_mock_ambiguous_violations
        )


# ===========================================================================
# 6. MeshCore metadata regression
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


# ===========================================================================
# 7. Classification subset alignment (Wave E)
# ===========================================================================


class TestClassificationSubsetAlignment:
    """NON_TERMINAL constants must appear in spec docs alongside terminal sets.

    Verify that state-machines.md §4.1 and delivery-lifecycle.md §2.1 both
    list every classification subset constant, including the new
    NON_TERMINAL_RECEIPT_STATUSES and NON_TERMINAL_OUTBOX_STATUSES.
    """

    # -- state-machines.md §4.1 classification table -------------------------

    def test_state_machines_lists_terminal_receipt(self) -> None:
        content = _read(STATE_MACHINES_MD)
        for status in TERMINAL_RECEIPT_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"state-machines.md missing terminal receipt status '{status}'"

    def test_state_machines_lists_non_terminal_receipt(self) -> None:
        content = _read(STATE_MACHINES_MD)
        for status in NON_TERMINAL_RECEIPT_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"state-machines.md missing non-terminal receipt status '{status}'"

    def test_state_machines_lists_terminal_outbox(self) -> None:
        content = _read(STATE_MACHINES_MD)
        for status in TERMINAL_OUTBOX_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"state-machines.md missing terminal outbox status '{status}'"

    def test_state_machines_lists_non_terminal_outbox(self) -> None:
        content = _read(STATE_MACHINES_MD)
        for status in NON_TERMINAL_OUTBOX_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"state-machines.md missing non-terminal outbox status '{status}'"

    # -- delivery-lifecycle.md §2.1 classification table ---------------------

    def test_delivery_lifecycle_mentions_non_terminal_receipt(self) -> None:
        content = _read(DELIVERY_LIFECYCLE_MD)
        for status in NON_TERMINAL_RECEIPT_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"delivery-lifecycle.md missing non-terminal receipt status '{status}'"

    def test_delivery_lifecycle_mentions_non_terminal_outbox(self) -> None:
        content = _read(DELIVERY_LIFECYCLE_MD)
        for status in NON_TERMINAL_OUTBOX_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"delivery-lifecycle.md missing non-terminal outbox status '{status}'"

    def test_delivery_lifecycle_mentions_claimable_outbox(self) -> None:
        content = _read(DELIVERY_LIFECYCLE_MD)
        for status in CLAIMABLE_OUTBOX_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"delivery-lifecycle.md missing claimable outbox status '{status}'"

    def test_delivery_lifecycle_mentions_accepted_outcome(self) -> None:
        content = _read(DELIVERY_LIFECYCLE_MD)
        for status in ACCEPTED_OUTCOME_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"delivery-lifecycle.md missing accepted outcome status '{status}'"

    # -- lifecycle-authority-audit.md classification table -------------------

    def test_audit_doc_mentions_non_terminal_receipt(self) -> None:
        content = _read(LIFECYCLE_AUDIT_MD)
        for status in NON_TERMINAL_RECEIPT_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"lifecycle-authority-audit.md missing non-terminal receipt status '{status}'"

    def test_audit_doc_mentions_non_terminal_outbox(self) -> None:
        content = _read(LIFECYCLE_AUDIT_MD)
        for status in NON_TERMINAL_OUTBOX_STATUSES:
            assert (
                f"`{status}`" in content
            ), f"lifecycle-authority-audit.md missing non-terminal outbox status '{status}'"

    # -- partition invariants documented -------------------------------------

    def test_state_machines_mentions_non_terminal_constant_name(self) -> None:
        """state-machines.md §4.1 must name NON_TERMINAL_RECEIPT_STATUSES and
        NON_TERMINAL_OUTBOX_STATUSES."""
        content = _read(STATE_MACHINES_MD)
        assert (
            "NON_TERMINAL_RECEIPT_STATUSES" in content
        ), "state-machines.md does not mention NON_TERMINAL_RECEIPT_STATUSES"
        assert (
            "NON_TERMINAL_OUTBOX_STATUSES" in content
        ), "state-machines.md does not mention NON_TERMINAL_OUTBOX_STATUSES"

    def test_delivery_lifecycle_mentions_non_terminal_constant_name(self) -> None:
        """delivery-lifecycle.md §2.1 must name NON_TERMINAL constants."""
        content = _read(DELIVERY_LIFECYCLE_MD)
        assert (
            "NON_TERMINAL_RECEIPT_STATUSES" in content
        ), "delivery-lifecycle.md does not mention NON_TERMINAL_RECEIPT_STATUSES"
        assert (
            "NON_TERMINAL_OUTBOX_STATUSES" in content
        ), "delivery-lifecycle.md does not mention NON_TERMINAL_OUTBOX_STATUSES"


# ===========================================================================
# 8. Transition table alignment (Wave E)
# ===========================================================================


class TestReceiptTransitionAlignment:
    """Receipt transition table in state-machines.md §1.3 must align with
    ``RECEIPT_TRANSITIONS`` in ``delivery_state.py``."""

    @staticmethod
    def _parse_receipt_transition_pairs() -> set[tuple[str, str]]:
        """Parse (source, target) pairs from state-machines.md §1.3 table rows.

        Returns a set of pairs.  Source ``—`` is mapped to the empty string
        to distinguish it from a real status name.
        """
        content = _read(STATE_MACHINES_MD)
        section_start = content.find("### 1.3 Legal Transitions")
        section_end = content.find("### 1.4")
        assert section_start != -1, "Cannot find §1.3 Legal Transitions"
        assert section_end != -1, "Cannot find §1.4"
        section = content[section_start:section_end]

        pairs: set[tuple[str, str]] = set()
        for line in section.splitlines():
            # Match markdown table rows: | source | target | ... |
            m = re.match(
                r"\|\s*(?:`([a-z_]+)`|—)\s*\|\s*`([a-z_]+)`\s*\|",
                line.strip(),
            )
            if m:
                source = m.group(1) or "—"
                target = m.group(2)
                pairs.add((source, target))
        return pairs

    def test_all_code_receipt_transitions_in_docs(self) -> None:
        """Every source→target pair in RECEIPT_TRANSITIONS must appear as an
        exact row in state-machines.md §1.3 legal transitions table."""
        docs_pairs = self._parse_receipt_transition_pairs()

        missing: list[str] = []
        for source, targets in RECEIPT_TRANSITIONS.items():
            for target in targets:
                if (source, target) not in docs_pairs:
                    missing.append(f"`{source}` → `{target}`")

        assert not missing, (
            "Receipt transition pairs from RECEIPT_TRANSITIONS not found "
            "as exact rows in §1.3 table:\n" + "\n".join(f"  {m}" for m in missing)
        )

    def test_all_docs_receipt_transitions_in_code(self) -> None:
        """Every source→target pair in §1.3 table must exist in
        RECEIPT_TRANSITIONS (docs→code coverage).

        Rows with source ``—`` are initial receipt insertions (no previous
        receipt), not state transitions.  They are excluded from this check
        because ``RECEIPT_TRANSITIONS`` only models transitions from an
        existing receipt status.
        """
        docs_pairs = self._parse_receipt_transition_pairs()

        # Build the set of all code pairs.
        code_pairs: set[tuple[str, str]] = set()
        for source, targets in RECEIPT_TRANSITIONS.items():
            for target in targets:
                code_pairs.add((source, target))

        extra: list[str] = []
        for source, target in docs_pairs:
            # Skip initial-receipt rows (source == "—"); they are not transitions.
            if source == "—":
                continue
            if (source, target) not in code_pairs:
                extra.append(f"`{source}` → `{target}`")

        assert not extra, (
            "Receipt transition rows in §1.3 table not found in "
            "RECEIPT_TRANSITIONS:\n" + "\n".join(f"  {e}" for e in extra)
        )

    def test_terminal_receipt_statuses_have_no_outgoing_in_docs(self) -> None:
        """Terminal receipt statuses must not appear as transition sources
        in state-machines.md §1.3."""
        docs_pairs = self._parse_receipt_transition_pairs()

        violations: list[str] = []
        for source, target in docs_pairs:
            if source in TERMINAL_RECEIPT_STATUSES:
                violations.append(f"`{source}` → `{target}`")

        assert not violations, (
            "Terminal receipt statuses found as transition sources in §1.3:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


class TestOutboxTransitionAlignment:
    """Outbox transition table in state-machines.md §2.3 must align with
    ``OUTBOX_TRANSITIONS`` in ``delivery_state.py``."""

    @staticmethod
    def _parse_outbox_transition_pairs() -> set[tuple[str, str]]:
        """Parse (source, target) pairs from state-machines.md §2.3 table rows.

        Returns a set of pairs.  Source ``—`` is mapped to the empty string.
        """
        content = _read(STATE_MACHINES_MD)
        section_start = content.find("### 2.3 Legal Transitions")
        section_end = content.find("### 2.4")
        assert section_start != -1, "Cannot find §2.3 Legal Transitions"
        assert section_end != -1, "Cannot find §2.4 Mutable"
        section = content[section_start:section_end]

        pairs: set[tuple[str, str]] = set()
        for line in section.splitlines():
            # Match: | source | target | method | condition |
            m = re.match(
                r"\|\s*(?:`([a-z_]+)`|—)\s*\|\s*`([a-z_]+)`\s*\|",
                line.strip(),
            )
            if m:
                source = m.group(1) or "—"
                target = m.group(2)
                pairs.add((source, target))
        return pairs

    def test_all_code_outbox_transitions_in_docs(self) -> None:
        """Every source→target pair in OUTBOX_TRANSITIONS must appear as an
        exact row in state-machines.md §2.3 legal transitions table."""
        docs_pairs = self._parse_outbox_transition_pairs()

        missing: list[str] = []
        for source, targets in OUTBOX_TRANSITIONS.items():
            for target in targets:
                if (source, target) not in docs_pairs:
                    missing.append(f"`{source}` → `{target}`")

        assert not missing, (
            "Outbox transition pairs from OUTBOX_TRANSITIONS not found "
            "as exact rows in §2.3 table:\n" + "\n".join(f"  {m}" for m in missing)
        )

    def test_all_docs_outbox_transitions_in_code(self) -> None:
        """Every source→target pair in §2.3 table must exist in
        OUTBOX_TRANSITIONS (docs→code coverage).

        Rows with source ``—`` are initial outbox insertions (no previous
        status), not state transitions.  They are excluded because
        ``OUTBOX_TRANSITIONS`` only models transitions from an existing status.
        """
        docs_pairs = self._parse_outbox_transition_pairs()

        code_pairs: set[tuple[str, str]] = set()
        for source, targets in OUTBOX_TRANSITIONS.items():
            for target in targets:
                code_pairs.add((source, target))

        extra: list[str] = []
        for source, target in docs_pairs:
            # Skip initial-insertion rows (source == "—"); they are not transitions.
            if source == "—":
                continue
            if (source, target) not in code_pairs:
                extra.append(f"`{source}` → `{target}`")

        assert not extra, (
            "Outbox transition rows in §2.3 table not found in "
            "OUTBOX_TRANSITIONS:\n" + "\n".join(f"  {e}" for e in extra)
        )

    def test_terminal_outbox_statuses_have_no_outgoing_in_docs(self) -> None:
        """Terminal outbox statuses must not appear as transition sources
        in state-machines.md §2.3 legal transitions table rows."""
        docs_pairs = self._parse_outbox_transition_pairs()

        violations: list[str] = []
        for source, target in docs_pairs:
            if source in TERMINAL_OUTBOX_STATUSES:
                violations.append(f"`{source}` → `{target}`")

        assert not violations, (
            "Terminal outbox statuses found as transition sources in §2.3:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# 9. Dead-letter attempt convention alignment (Wave E)
# ===========================================================================


class TestDeadLetterAttemptConvention:
    """Dead-letter attempt_number convention must be documented in
    state-machines.md."""

    def test_state_machines_documents_dead_letter_convention(self) -> None:
        """state-machines.md must have §1.6 Dead-Letter Attempt Convention."""
        content = _read(STATE_MACHINES_MD)
        assert (
            "Dead-Letter Attempt Convention" in content
        ), "state-machines.md missing Dead-Letter Attempt Convention section"
        assert "attempt_number + 1" in content, (
            "state-machines.md dead-letter convention does not describe "
            "attempt_number + 1 chain-closing"
        )

    def test_audit_doc_documents_dead_letter_convention(self) -> None:
        """lifecycle-authority-audit.md must document the convention."""
        content = _read(LIFECYCLE_AUDIT_MD)
        assert "attempt_number = N + 1" in content, (
            "lifecycle-authority-audit.md missing dead-letter attempt "
            "convention note"
        )
