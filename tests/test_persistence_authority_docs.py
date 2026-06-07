"""Persistence authority tests: documentation consistency.

Focused tests proving documentation consistency where gaps exist from
Waves 1-2, without duplicating existing near-limit test files.

Covers:
  1. Spec-planned identity/archive tables are documented as not current DDL.
  2. storage.md ownership section exists and references correct tables.
  3. persistence-authority-audit.md exists and states no schema bump.
  4. _EXPECTED_SCHEMA_VERSION is 1 (cross-checked with docs).
  5. No schema bump language implied in docs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from medre.core.storage.sqlite.schema import (
    _EXPECTED_SCHEMA_VERSION,
    _REQUIRED_COLUMNS,
    _SCHEMA,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
_STORAGE_SPEC = _DOCS_DIR / "spec" / "storage.md"
_PERSISTENCE_AUDIT = _DOCS_DIR / "dev" / "persistence-authority-audit.md"


# ===================================================================
# 1. Spec-planned tables not in current DDL
# ===================================================================


class TestSpecPlannedTablesNotInDDL:
    """Spec-planned identity and archive tables are documented in storage.md
    but must NOT appear in the current DDL or _REQUIRED_COLUMNS.

    This ensures documentation accurately reflects implementation state:
    these tables are planned for the future but do not exist yet.
    """

    SPEC_PLANNED_TABLES = (
        "actors",
        "native_identities",
        "actor_identity_links",
        "actor_permissions",
        "native_archive",
    )

    @pytest.mark.parametrize("table_name", SPEC_PLANNED_TABLES)
    def test_spec_planned_table_not_in_ddl(self, table_name: str) -> None:
        """Spec-planned tables must not appear in _SCHEMA DDL."""
        pattern = (
            rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table_name)}\b"
        )
        assert not re.search(pattern, _SCHEMA, re.IGNORECASE), (
            f"Spec-planned table {table_name!r} found in _SCHEMA DDL — "
            f"remove from DDL or update docs to remove 'spec-planned' label"
        )

    @pytest.mark.parametrize("table_name", SPEC_PLANNED_TABLES)
    def test_spec_planned_table_not_in_required_columns(self, table_name: str) -> None:
        """Spec-planned tables must not appear in _REQUIRED_COLUMNS."""
        assert table_name not in _REQUIRED_COLUMNS, (
            f"Spec-planned table {table_name!r} found in _REQUIRED_COLUMNS — "
            f"remove from required columns or update docs"
        )

    def test_storage_md_documents_spec_planned_tables(self) -> None:
        """storage.md must document spec-planned tables with a 'not implemented' note."""
        content = _STORAGE_SPEC.read_text()
        for table in self.SPEC_PLANNED_TABLES:
            assert (
                table in content
            ), f"storage.md must document spec-planned table {table!r}"

    def test_persistence_audit_labels_spec_planned(self) -> None:
        """persistence-authority-audit.md labels spec-planned tables correctly."""
        content = _PERSISTENCE_AUDIT.read_text()
        assert "spec-planned" in content.lower() or "not implemented" in content.lower()

    def test_storage_md_separates_current_ddl_from_spec_planned(self) -> None:
        """storage.md distinguishes current implementation tables from spec-planned ones.

        The document must explicitly mark spec-planned tables as not part of
        the current DDL, so readers do not assume they exist at runtime.
        """
        content = _STORAGE_SPEC.read_text()
        assert (
            "not part of the current" in content.lower()
            or "spec-planned" in content.lower()
        ), (
            "storage.md must contain phrasing that distinguishes current DDL "
            "tables from spec-planned ones (e.g., 'not part of the current DDL')"
        )


# ===================================================================
# 2. storage.md ownership section exists
# ===================================================================


class TestStorageMDOwnershipSection:
    """storage.md must have an ownership section documenting write/mutation
    authority for each table.
    """

    def test_storage_md_exists(self) -> None:
        """storage.md spec file exists."""
        assert _STORAGE_SPEC.is_file()

    def test_ownership_section_exists(self) -> None:
        """storage.md contains a storage ownership section."""
        content = _STORAGE_SPEC.read_text()
        # Look for section heading with 'ownership' in it
        assert re.search(
            r"#.*ownership", content, re.IGNORECASE
        ), "storage.md must contain a section heading with 'ownership'"

    @pytest.mark.parametrize(
        "table",
        (
            "canonical_events",
            "delivery_receipts",
            "native_message_refs",
            "delivery_outbox",
            "delivery_status",
        ),
    )
    def test_core_tables_mentioned_in_spec(self, table: str) -> None:
        """Core tables are mentioned in storage.md."""
        content = _STORAGE_SPEC.read_text()
        assert table in content, f"storage.md must mention table {table!r}"

    @pytest.mark.parametrize(
        "table",
        (
            "canonical_events",
            "delivery_receipts",
            "native_message_refs",
            "delivery_outbox",
        ),
    )
    def test_core_table_delete_authority_is_none(self, table: str) -> None:
        """Core tables have 'None' delete authority in storage.md ownership table."""
        content = _STORAGE_SPEC.read_text()
        # Find ownership table rows (markdown lines with pipes) mentioning this table
        matching_rows = [
            line for line in content.splitlines() if table in line and "|" in line
        ]
        assert (
            matching_rows
        ), f"storage.md must have an ownership table row for {table!r}"
        # At least one matching row must state 'None' as delete authority
        assert any(
            "none" in row.lower() for row in matching_rows
        ), f"storage.md ownership row for {table} must state 'None' as delete authority"

    def test_append_only_guarantee_stated(self) -> None:
        """storage.md states the append-only guarantee."""
        content = _STORAGE_SPEC.read_text().lower()
        assert "append-only" in content or "append only" in content

    def test_native_message_refs_creator_is_core_pipeline(self) -> None:
        """storage.md ownership table credits Core pipeline as native_message_refs creator.

        Adapters report native-to-canonical correlation facts but the Core
        pipeline/runtime owns the persistence via store_native_ref().
        The Creator column must NOT list "Adapters" as the sole creator.
        """
        content = _STORAGE_SPEC.read_text()
        matching_rows = [
            line
            for line in content.splitlines()
            if "native_message_refs" in line and "|" in line
        ]
        assert (
            matching_rows
        ), "storage.md must have an ownership table row for 'native_message_refs'"
        # At least one row must reference Core pipeline as the creator/owner
        assert any(
            "Core pipeline" in row or "core pipeline" in row for row in matching_rows
        ), (
            "storage.md ownership row for native_message_refs must credit "
            "'Core pipeline/runtime' as creator, not Adapters as sole creator"
        )


# ===================================================================
# 3. persistence-authority-audit.md consistency
# ===================================================================


class TestPersistenceAuditDocConsistency:
    """persistence-authority-audit.md must be consistent with storage.md
    and the source code.
    """

    def test_audit_doc_exists(self) -> None:
        """persistence-authority-audit.md exists."""
        assert _PERSISTENCE_AUDIT.is_file()

    def test_audit_doc_references_storage_md(self) -> None:
        """Audit doc references storage.md as the normative authority."""
        content = _PERSISTENCE_AUDIT.read_text()
        assert "storage.md" in content

    def test_audit_doc_states_no_schema_bump(self) -> None:
        """Audit doc explicitly states no schema bump is required."""
        content = _PERSISTENCE_AUDIT.read_text().lower()
        # The doc should state it does not imply/require a schema bump
        assert (
            "schema bump" in content
            or "no schema" in content
            or "does not imply" in content
        )

    def test_audit_doc_states_no_delete(self) -> None:
        """Audit doc states the actual no-runtime-delete policy, not just mentions DELETE."""
        content = _PERSISTENCE_AUDIT.read_text()
        # Must contain the specific policy phrase, not just the word "DELETE"
        # in any context.  The doc uses "no runtime `DELETE FROM` on any table"
        # so match "no runtime" within a few characters of "delete".
        assert re.search(r"no runtime .{0,5}delete", content, re.IGNORECASE), (
            "Audit doc must state the no-runtime-delete policy "
            "(e.g., 'no runtime DELETE FROM on any table')"
        )

    def test_audit_doc_version_matches_source(self) -> None:
        """Audit doc's schema version statement matches _EXPECTED_SCHEMA_VERSION."""
        content = _PERSISTENCE_AUDIT.read_text()
        assert str(_EXPECTED_SCHEMA_VERSION) in content

    def test_audit_native_message_refs_owner_is_core_pipeline(self) -> None:
        """Audit doc native_message_refs row credits Core pipeline as write authority.

        Adapters report native facts but Core pipeline owns the persistence.
        The Write authority (or Owner) column must reference Core pipeline,
        not list Adapters as the sole persistence owner.
        """
        content = _PERSISTENCE_AUDIT.read_text()
        matching_rows = [
            line
            for line in content.splitlines()
            if "native_message_refs" in line and "|" in line
        ]
        assert (
            matching_rows
        ), "Audit doc must have a domain table row for 'native_message_refs'"
        # At least one row must reference Core pipeline as the write/owner authority
        assert any(
            "Core pipeline" in row or "core pipeline" in row for row in matching_rows
        ), (
            "Audit doc native_message_refs row must credit 'Core pipeline' "
            "as write authority, not Adapters as sole owner"
        )

    def test_audit_replay_section_mentions_delivery_outbox(self) -> None:
        """Audit doc replay section mentions delivery_outbox in context of pipeline delegation.

        BEST_EFFORT replay triggers full pipeline re-execution. The replay
        semantics section must acknowledge delivery_outbox rows may be created
        via pipeline (even if replay delegates rather than writes directly).
        """
        content = _PERSISTENCE_AUDIT.read_text()
        # Find the replay persistence semantics section (non-greedy: stop at next ##)
        replay_section_match = re.search(
            r"(?i)replay persistence semantics.*?(?=\n##|\Z)",
            content,
            re.DOTALL,
        )
        assert (
            replay_section_match
        ), "Audit doc must have a 'Replay Persistence Semantics' section"
        replay_section = replay_section_match.group()
        assert "delivery_outbox" in replay_section, (
            "Audit doc replay section must mention delivery_outbox "
            "(e.g., replay delegates to pipeline which may create outbox rows)"
        )

    def test_audit_doc_does_not_claim_all_operator_commands_readonly(self) -> None:
        """Audit doc must not blanket-assert all operator commands are read-only.

        `medre smoke` and `medre run-session` are executable runtime workflows
        that may write to the database through normal pipeline authorities when
        persistent storage is configured.
        """
        content = _PERSISTENCE_AUDIT.read_text()
        assert "no operator command writes" not in content.lower(), (
            "Audit doc must not claim 'no operator command writes to the database' — "
            "medre smoke and run-session may write through normal pipeline authorities"
        )

    @pytest.mark.parametrize(
        "read_only_cmd",
        (
            "medre inspect",
            "medre evidence",
            "medre trace",
            "medre recover",
        ),
    )
    def test_audit_doc_lists_readonly_operator_commands(
        self, read_only_cmd: str
    ) -> None:
        """Audit doc operator visibility section lists read-only inspection commands."""
        content = _PERSISTENCE_AUDIT.read_text()
        assert read_only_cmd in content, (
            f"Audit doc must list {read_only_cmd!r} as a read-only operator command "
            f"in the operator visibility section"
        )

    def test_audit_doc_mentions_smoke_as_runtime_workflow(self) -> None:
        """Audit doc mentions medre smoke as a runtime workflow that may write.

        `medre smoke` is an executable runtime workflow (not just a passive
        diagnostic). When persistent storage is configured, smoke may trigger
        writes through the normal pipeline authority path.
        """
        content = _PERSISTENCE_AUDIT.read_text()
        assert (
            "medre smoke" in content
        ), "Audit doc operator visibility section must mention 'medre smoke'"


# ===================================================================
# 4. Schema version consistency
# ===================================================================


class TestSchemaVersionDocConsistency:
    """Schema version in source, docs, and audit must be consistent."""

    def test_version_is_1_everywhere(self) -> None:
        """Schema version is 1 in source code."""
        assert _EXPECTED_SCHEMA_VERSION == 1

    def test_storage_md_mentions_version_1(self) -> None:
        """storage.md references schema version 1."""
        content = _STORAGE_SPEC.read_text()
        # Look for schema version reference
        assert "schema_version" in content or "schema version" in content.lower()

    def test_no_migration_language_in_docs(self) -> None:
        """Docs do not contain migration/migrate language that implies a bump."""
        content = _PERSISTENCE_AUDIT.read_text()
        # Every line containing "migration" must also contain "no" / "not" /
        # "no auto-migration" within the same sentence (negative context).
        for line in content.splitlines():
            if "migration" in line.lower():
                assert re.search(
                    r"(no|not).{0,50}migration", line, re.IGNORECASE
                ), f"Migration mentioned outside negative context: {line.strip()}"


# ===================================================================
# 5. No schema bump language in docs
# ===================================================================


class TestNoSchemaBumpLanguage:
    """Documentation must not imply or suggest a schema bump is needed."""

    def test_audit_doc_no_implied_bump(self) -> None:
        """Audit doc does not imply a schema version change."""
        content = _PERSISTENCE_AUDIT.read_text().lower()
        # Should NOT contain language like "requires schema bump" or "version 2"
        bump_patterns = [
            r"requires?\s+a?\s*schema\s+bump",
            r"schema\s+version\s+2",
            r"bump\s+schema",
            r"increment\s+schema\s+version",
        ]
        for pattern in bump_patterns:
            assert not re.search(
                pattern, content
            ), f"Audit doc implies schema bump: matched {pattern!r}"

    def test_storage_md_no_implied_bump(self) -> None:
        """storage.md does not imply a schema version change."""
        content = _STORAGE_SPEC.read_text().lower()
        bump_patterns = [
            r"requires?\s+a?\s*schema\s+bump",
            r"schema\s+version\s+2",
            r"bump\s+schema",
            r"increment\s+schema\s+version",
        ]
        for pattern in bump_patterns:
            assert not re.search(
                pattern, content
            ), f"storage.md implies schema bump: matched {pattern!r}"
