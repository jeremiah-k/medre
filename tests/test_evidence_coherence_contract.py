"""Coherence contract tests for the evidence and convergence surface.

MEDRE has two distinct surfaces that share the "evidence bundle" name:

* :class:`~medre.core.evidence.bundle.EvidenceBundle` — a frozen per-event
  :class:`msgspec.Struct` produced by
  :class:`~medre.core.evidence.collector.EvidenceCollector`.
* The runtime evidence dict — a plain ``dict[str, Any]`` returned by
  :func:`medre.runtime.evidence.collect_evidence_bundle` and consumed by
  the ``medre evidence`` CLI.

The JSON Schema in ``docs/schemas/evidence-bundle.schema.json`` describes
the **runtime dict** surface.  The per-event struct has additional
per-event fields (``event_id``, ``event_summary``, ``delivery_receipts``,
``native_refs``, ``outbox_items``, ``replay_run_ids``, ``sources_seen``,
``warnings``) that the schema does not enumerate; those are documented
in the struct's class docstring and tested at the type level.

This module pins the contract that keeps these surfaces from drifting:

1. The example file validates against the schema.
2. Runtime dicts produced by :func:`collect_evidence_bundle` validate
   against the schema in both ``config_path`` and ``storage_path`` modes.
3. The status vocabulary frozensets duplicated across modules are
   identical to the canonical source in
   :mod:`medre.core.engine.pipeline.delivery_state`.
4. Schema enum values match the code constants for convergence severity,
   orphan finding kinds, lifecycle finding kinds, recovery ownership
   actions, recovery sources, and shutdown status.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _ROOT / "docs" / "schemas" / "evidence-bundle.schema.json"
_EXAMPLE_PATH = _ROOT / "docs" / "schemas" / "examples" / "evidence-bundle-example.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_enums(
    schema: dict[str, Any],
    def_name: str,
    prop_name: str | None = None,
) -> list[str]:
    """Extract the ``enum`` list for a property inside a named ``$def``.

    If ``prop_name`` is given, return the enum for that specific property.
    Otherwise, return the enum for the first property that has one
    (preserved for backward compatibility with single-enum defs).
    """
    defn = schema["$defs"][def_name]
    if prop_name is not None:
        prop = defn["properties"][prop_name]
        if "enum" not in prop:
            raise AssertionError(f"$defs/{def_name}.{prop_name} has no enum property")
        return list(prop["enum"])
    for _prop_name, prop in defn["properties"].items():
        if "enum" in prop:
            return list(prop["enum"])
    raise AssertionError(f"$defs/{def_name} has no enum property")


# ===========================================================================
# 1. Example validates against schema
# ===========================================================================


class TestExampleValidatesAgainstSchema:
    """The static example must validate against the schema."""

    def test_example_is_valid_json(self) -> None:
        json.loads(_EXAMPLE_PATH.read_text(encoding="utf-8"))

    def test_example_validates_against_schema(self) -> None:
        import jsonschema

        example = _load_json(_EXAMPLE_PATH)
        schema = _load_json(_SCHEMA_PATH)
        jsonschema.validate(instance=example, schema=schema)

    def test_example_includes_every_optional_top_level_key(self) -> None:
        """The example must demonstrate every schema-optional top-level key,
        even if some are ``null``.  This keeps the example a faithful
        template for operators who want to see the full shape."""
        example = _load_json(_EXAMPLE_PATH)
        optional_keys = {
            "convergence_summary",
            "orphan_report",
            "recovery_summary",
            "recovery_ledger",
            "lifecycle_convergence_report",
        }
        missing = optional_keys - set(example.keys())
        assert not missing, f"example missing optional keys: {sorted(missing)}"


# ===========================================================================
# 2. Runtime dict validates against schema (config and storage_path modes)
# ===========================================================================


class TestRuntimeDictValidatesAgainstSchema:
    """The runtime bundle dict produced by ``collect_evidence_bundle`` must
    satisfy the JSON Schema in both configuration modes."""

    @pytest.fixture
    def schema(self) -> dict[str, Any]:
        return _load_json(_SCHEMA_PATH)

    def _assert_validates(self, bundle: dict[str, Any], schema: dict[str, Any]) -> None:
        import jsonschema

        # The schema's ``additionalProperties`` is ``false`` at the top level.
        # We allow any extra keys to be flagged, not silently accepted.
        jsonschema.validate(instance=bundle, schema=schema)

    @pytest.mark.asyncio
    async def test_storage_path_empty_db_validates(
        self, tmp_path: Path, schema: dict[str, Any]
    ) -> None:
        """A bundle collected from a non-existent or empty database must
        still validate against the schema.  This catches the case where
        the runtime introduces a new top-level key without updating the
        schema."""
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        # Non-existent db path → partial section but top-level still valid.
        db = tmp_path / "does-not-exist.sqlite"
        bundle = await collect_evidence_bundle(storage_path=str(db))
        self._assert_validates(bundle, schema)

    @pytest.mark.asyncio
    async def test_storage_path_with_init_db_validates(
        self, tmp_path: Path, schema: dict[str, Any]
    ) -> None:
        """A bundle collected from a freshly-initialised sqlite storage
        must validate.  This exercises the ``no event`` / ``no receipts``
        case at the top level."""
        from medre.core.storage.sqlite.storage import SQLiteStorage
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        db = tmp_path / "state.sqlite"
        storage = SQLiteStorage(db_path=str(db))
        await storage.initialize()
        await storage.close()

        bundle = await collect_evidence_bundle(storage_path=str(db))
        self._assert_validates(bundle, schema)

    def test_config_error_bundle_validates(self, schema: dict[str, Any]) -> None:
        """When config loading fails outright, ``collect_evidence_bundle``
        returns an error bundle.  This bundle must still satisfy the
        schema (it's the canonical 'config error' operator surface)."""
        import asyncio

        from medre.runtime.evidence._bundle import collect_evidence_bundle

        # Call the real function with a path that will fail config loading.
        # This ensures the test stays in sync with the actual error shape
        # — if the function's error branch changes, this test catches it.
        bundle = asyncio.run(
            collect_evidence_bundle(
                config_path="/nonexistent/path/that/does/not/exist.toml"
            )
        )
        assert bundle["status"] == "error"
        self._assert_validates(bundle, schema)

    @pytest.mark.asyncio
    async def test_config_backed_bundle_validates(
        self, tmp_path: Path, schema: dict[str, Any]
    ) -> None:
        """A bundle collected via ``config_path`` mode must validate
        against the schema.  This is the primary operator use case
        (``medre evidence --config my-bridge.toml --json``)."""
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        # Use a minimal valid config that will load but produce a simple
        # bundle.  The shipped fake-bridge-smoke.toml is a good candidate.
        config_path = _ROOT / "examples" / "configs" / "fake-bridge-smoke.toml"
        if not config_path.exists():
            pytest.skip("fake-bridge-smoke.toml not available")

        bundle = await collect_evidence_bundle(config_path=str(config_path))
        self._assert_validates(bundle, schema)


# ===========================================================================
# 3. Status vocabulary coherence
# ===========================================================================


class TestStatusVocabularyCoherence:
    """Status vocabulary frozensets duplicated across modules must be
    identical to the canonical source in
    :mod:`medre.core.engine.pipeline.delivery_state`.  When someone
    adds a new terminal outbox status, the test fails unless the
    corresponding frozensets are updated in lockstep."""

    def test_convergence_helpers_match_canonical(self) -> None:
        from medre.core.diagnostics.convergence import helpers
        from medre.core.engine.pipeline import delivery_state

        assert helpers._TERMINAL_OUTBOX == delivery_state.TERMINAL_OUTBOX_STATUSES
        assert helpers._NON_TERMINAL_OUTBOX == (
            delivery_state.OUTBOX_STATUSES - delivery_state.TERMINAL_OUTBOX_STATUSES
        )
        assert helpers._TERMINAL_RECEIPT == delivery_state.TERMINAL_RECEIPT_STATUSES
        assert helpers._NON_TERMINAL_RECEIPT == (
            delivery_state.RECEIPT_STATUSES - delivery_state.TERMINAL_RECEIPT_STATUSES
        )

    def test_retry_outbox_vocabulary_matches_canonical(self) -> None:
        from medre.core.engine.pipeline import delivery_state
        from medre.core.evidence import retry_outbox

        assert retry_outbox._TERMINAL_OUTBOX == delivery_state.TERMINAL_OUTBOX_STATUSES
        assert retry_outbox._NON_TERMINAL_OUTBOX == (
            delivery_state.OUTBOX_STATUSES - delivery_state.TERMINAL_OUTBOX_STATUSES
        )

    def test_recovery_classification_vocabulary_matches_canonical(self) -> None:
        from medre.core.engine.pipeline import delivery_state
        from medre.core.recovery import classification

        assert (
            classification._TERMINAL_STATUSES == delivery_state.TERMINAL_OUTBOX_STATUSES
        )
        assert (
            classification._NON_TERMINAL_STATUSES
            == delivery_state.OUTBOX_STATUSES - delivery_state.TERMINAL_OUTBOX_STATUSES
        )

    def test_shutdown_pending_outbox_vocabulary_matches_canonical(self) -> None:
        from medre.core.engine.pipeline import delivery_state
        from medre.core.evidence import shutdown

        assert shutdown._PENDING_OUTBOX_STATUSES == (
            delivery_state.OUTBOX_STATUSES - delivery_state.TERMINAL_OUTBOX_STATUSES
        )

    def test_shutdown_classification_maps_cover_all_outbox_statuses(self) -> None:
        """The static classification maps in ``shutdown.py`` must cover
        every outbox status from the canonical vocabulary.  If a new
        outbox status is added to ``delivery_state``, this test fails
        unless the classification maps are updated."""
        from medre.core.engine.pipeline import delivery_state
        from medre.core.evidence import shutdown

        resumable_keys = set(shutdown._RESUMABLE_OUTBOX_CLASSIFICATIONS.keys())
        terminal_keys = set(shutdown._TERMINAL_OUTBOX_CLASSIFICATIONS.keys())
        outbox_statuses = delivery_state.OUTBOX_STATUSES
        # Resumable + terminal should cover all outbox statuses.
        assert resumable_keys | terminal_keys == outbox_statuses, (
            f"Classification maps missing statuses: "
            f"{outbox_statuses - (resumable_keys | terminal_keys)}"
        )
        # Resumable and terminal should not overlap.
        assert not (
            resumable_keys & terminal_keys
        ), f"Classification maps overlap: {resumable_keys & terminal_keys}"
        # Resumable should equal non-terminal outbox statuses.
        assert resumable_keys == (
            outbox_statuses - delivery_state.TERMINAL_OUTBOX_STATUSES
        )

    def test_sqlite_outbox_terminal_sets_match_canonical(self) -> None:
        """``_outbox.py`` must use the canonical terminal/claimable sets
        from ``delivery_state``, not local copies."""
        from medre.core.engine.pipeline import delivery_state
        from medre.core.storage.sqlite._outbox import _OutboxMixin

        # The module uses ``_terminal`` and ``_reclaimable`` as local
        # aliases inside ``create_outbox_item``.  Verify the canonical
        # sets are the values being used (no local frozenset duplication).
        assert delivery_state.TERMINAL_OUTBOX_STATUSES == frozenset(
            {"sent", "dead_lettered", "cancelled", "abandoned"}
        )
        assert delivery_state.CLAIMABLE_OUTBOX_STATUSES == frozenset(
            {"pending", "retry_wait"}
        )
        # The _outbox module must import from delivery_state, not define
        # its own frozensets.  ``create_outbox_item`` is on the
        # ``_OutboxMixin`` class, not the module directly.
        import inspect

        source = inspect.getsource(_OutboxMixin.create_outbox_item)
        assert (
            "frozenset" not in source
        ), "_OutboxMixin.create_outbox_item should not define local frozensets"
        assert delivery_state.CLAIMABLE_OUTBOX_STATUSES == frozenset(
            {"pending", "retry_wait"}
        )
        # The _outbox module must import from delivery_state, not define
        # its own frozensets.  ``create_outbox_item`` is on the
        # ``_OutboxMixin`` class, not the module directly.
        import inspect

        source = inspect.getsource(_OutboxMixin.create_outbox_item)
        assert (
            "frozenset" not in source
        ), "_OutboxMixin.create_outbox_item should not define local frozensets"

    def test_delivery_ledger_terminal_subset_matches_canonical(self) -> None:
        """``delivery_ledger._TERMINAL_STATUSES`` is intentionally broader
        (includes ``suppressed`` from receipt statuses).  The outbox-only
        subset must match ``TERMINAL_OUTBOX_STATUSES`` exactly."""
        from medre.core.engine.pipeline import delivery_state
        from medre.core.evidence import delivery_ledger

        # The outbox-relevant subset of _TERMINAL_STATUSES must equal
        # the canonical terminal outbox set.
        outbox_terminals = (
            delivery_ledger._TERMINAL_STATUSES & delivery_state.OUTBOX_STATUSES
        )
        assert outbox_terminals == delivery_state.TERMINAL_OUTBOX_STATUSES

    def test_retry_outbox_receipt_only_statuses_derivation(self) -> None:
        """``_RECEIPT_ONLY_STATUSES`` is derived from receipt statuses
        minus terminal outbox statuses minus ``{queued, sent}``.  Verify
        the derivation is consistent with the canonical vocabularies."""
        from medre.core.engine.pipeline import delivery_state
        from medre.core.evidence import retry_outbox

        expected = (
            delivery_state.RECEIPT_STATUSES
            - delivery_state.TERMINAL_OUTBOX_STATUSES
            - frozenset({"queued", "sent"})
        )
        assert retry_outbox._RECEIPT_ONLY_STATUSES == expected


# ===========================================================================
# 4. Schema enums match code constants
# ===========================================================================


class TestSchemaEnumsMatchCode:
    """The JSON Schema enums must equal the values produced by the
    corresponding :class:`enum.StrEnum` or string constants in the code."""

    def test_orphan_finding_kinds(self) -> None:
        from medre.core.diagnostics.convergence import types

        code_kinds = {
            types.KIND_ORPHANED_OUTBOX,
            types.KIND_ORPHANED_PARENT_RECEIPT,
            types.KIND_CROSS_PLAN_PARENT,
            types.KIND_CROSS_EVENT_PARENT,
            types.KIND_MISSING_DELIVERY_PLAN_ID,
            types.KIND_DEAD_LETTERED_RETRYABLE_MISMATCH,
            types.KIND_RECOVERED_NOT_PROGRESSED,
            types.KIND_REPEATEDLY_RECLAIMED,
            types.KIND_RECLAIMED_THEN_TERMINAL,
            types.KIND_RECLAIMED_THEN_ORPHANED,
        }
        schema = _load_json(_SCHEMA_PATH)
        schema_kinds = set(_schema_enums(schema, "OrphanFinding"))
        assert code_kinds == schema_kinds

    def test_lifecycle_finding_kinds(self) -> None:
        from medre.core.diagnostics.convergence import types

        code_kinds = {
            types.KIND_RECEIPT_OUTBOX_MISMATCH,
            types.KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX,
            types.KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT,
            types.KIND_RETRY_WAIT_MISSING_NEXT_RETRY,
            types.KIND_NEXT_RETRY_IN_PAST,
            types.KIND_RETRYABLE_WITHOUT_RETRY_METADATA,
            types.KIND_STALLED_DELIVERY_PLAN,
            types.KIND_ATTEMPT_COUNT_REGRESSION,
            types.KIND_RECEIPT_SEQUENCE_GAP,
        }
        schema = _load_json(_SCHEMA_PATH)
        schema_kinds = set(_schema_enums(schema, "LifecycleConvergenceFinding"))
        assert code_kinds == schema_kinds

    def test_recovery_ownership_actions(self) -> None:
        from medre.core.recovery.models import RecoveryOwnershipStatus

        code_actions = {member.value for member in RecoveryOwnershipStatus}
        schema = _load_json(_SCHEMA_PATH)
        schema_actions = set(_schema_enums(schema, "RecoveryOwnershipAction"))
        assert code_actions == schema_actions

    def test_recovery_sources(self) -> None:
        from medre.core.recovery.recovery_source import RecoverySource

        code_sources = {member.value for member in RecoverySource}
        schema = _load_json(_SCHEMA_PATH)
        schema_sources = set(
            _schema_enums(
                schema, "RecoveryOwnershipAction", prop_name="recovery_source"
            )
        )
        assert code_sources == schema_sources

    def test_convergence_severity_enum(self) -> None:
        from medre.core.diagnostics.convergence.types import ConvergenceSeverity

        code_severities = {member.value for member in ConvergenceSeverity}
        schema = _load_json(_SCHEMA_PATH)
        # ConvergenceSummary.worst_severity
        cs = schema["$defs"]["ConvergenceSummary"]["properties"]["worst_severity"]
        schema_severities = {v for v in cs["enum"] if v is not None}
        assert code_severities == schema_severities

    def test_shutdown_status_enum(self) -> None:
        from medre.core.evidence.shutdown import ShutdownStatus

        code_statuses = {member.value for member in ShutdownStatus}
        schema = _load_json(_SCHEMA_PATH)
        schema_statuses = set(_schema_enums(schema, "ShutdownEvidence"))
        assert code_statuses == schema_statuses


# ===========================================================================
# 5. Core EvidenceBundle contract
# ===========================================================================


class TestCoreEvidenceBundleContract:
    """The per-event :class:`EvidenceBundle` struct has a stable field
    contract documented in its class docstring.  Each field must be
    declared with a non-default or documented default to keep the
    deterministic-by-construction guarantee."""

    def test_struct_fields_have_docstrings(self) -> None:
        """Every field on :class:`EvidenceBundle` must be documented in
        the Attributes block of the class docstring.  Catches the case
        where someone adds a field without updating the public doc."""
        from medre.core.evidence.bundle import EvidenceBundle

        docstring = EvidenceBundle.__doc__ or ""
        # The Attributes block is delimited by "Attributes" and the
        # closing of the docstring (the next blank-line-then-non-indented
        # section).  In practice, every field name should appear in the
        # docstring as a field name.
        for field in EvidenceBundle.__struct_fields__:  # type: ignore[attr-defined]
            assert field in docstring, (
                f"EvidenceBundle field {field!r} is not documented in the "
                f"class docstring's Attributes block"
            )

    def test_struct_is_frozen(self) -> None:
        """The per-event struct must remain frozen to preserve the
        deterministic-by-construction guarantee."""
        from medre.core.evidence.bundle import EvidenceBundle

        # msgspec.Struct with frozen=True is encoded in the type itself.
        # We rely on the field-default immutability contract: trying to
        # assign a new value to a field on a frozen instance raises.
        bundle = EvidenceBundle(event_id="x")
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            bundle.event_id = "y"  # type: ignore[misc]

    def test_to_dict_returns_all_fields(self) -> None:
        """``EvidenceBundle.to_dict()`` must produce a dict whose keys
        match ``__struct_fields__`` exactly.  This guards against the
        case where someone adds a field but forgets the serializer."""
        from medre.core.evidence.bundle import EvidenceBundle

        bundle = EvidenceBundle(event_id="x", generated_at="2026-01-01T00:00:00Z")
        d = bundle.to_dict()
        assert set(d.keys()) == set(EvidenceBundle.__struct_fields__)  # type: ignore[attr-defined]


# ===========================================================================
# 6. Top-level convergence fields populated when event_id is provided
# ===========================================================================


class TestTopLevelConvergenceFieldsPopulated:
    """When ``collect_evidence_bundle`` is called with an ``event_id``
    and the database contains a real event with receipts/outbox items,
    the three per-event diagnostics fields must be populated at the
    top level of the runtime dict (not just nested in section data)."""

    @pytest.mark.asyncio
    async def test_top_level_convergence_fields_match_section_data(
        self, tmp_path: Path
    ) -> None:
        """The top-level ``convergence_summary``, ``orphan_report``, and
        ``lifecycle_convergence_report`` must be reference-identical to
        the same keys in ``sections.storage.data`` when an event is
        in scope."""
        from medre.core.storage.sqlite.storage import SQLiteStorage
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        # Create a DB, insert a minimal event + outbox + receipt, then
        # collect the bundle with event_id.
        db = tmp_path / "state.sqlite"
        storage = SQLiteStorage(db_path=str(db))
        await storage.initialize()

        # Insert a minimal event and outbox item so convergence has data.
        from datetime import datetime, timezone

        from medre.core.storage.backend import (
            CanonicalEvent,
            DeliveryOutboxItem,
            DeliveryReceipt,
        )

        event = CanonicalEvent(
            event_id="evt_test_001",
            event_kind="text",
            schema_version=1,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_adapter="bot",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata={},
        )
        await storage.append(event)

        outbox = DeliveryOutboxItem(
            outbox_id="obx_001",
            event_id="evt_test_001",
            route_id="route-001",
            delivery_plan_id="plan_001",
            target_adapter="radio",
            target_channel="chan_a",
            status="sent",
            attempt_number=1,
        )
        await storage.create_outbox_item(outbox)

        receipt = DeliveryReceipt(
            receipt_id="rcpt_001",
            event_id="evt_test_001",
            delivery_plan_id="plan_001",
            target_adapter="radio",
            target_channel="chan_a",
            status="sent",
            attempt_number=1,
        )
        await storage.append_receipt(receipt)
        await storage.close()

        bundle = await collect_evidence_bundle(
            storage_path=str(db), event_id="evt_test_001"
        )

        # Top-level fields must be populated (not None).
        assert (
            bundle["convergence_summary"] is not None
        ), "convergence_summary must be populated at top level when event_id is provided"
        assert (
            bundle["orphan_report"] is not None
        ), "orphan_report must be populated at top level when event_id is provided"
        assert (
            bundle["lifecycle_convergence_report"] is not None
        ), "lifecycle_convergence_report must be populated at top level when event_id is provided"

        # Top-level fields must be reference-identical to section data.
        storage_data = bundle["sections"]["storage"]["data"]
        assert bundle["convergence_summary"] is storage_data["convergence_summary"]
        assert bundle["orphan_report"] is storage_data["orphan_report"]
        assert (
            bundle["lifecycle_convergence_report"]
            is storage_data["lifecycle_convergence_report"]
        )

    @pytest.mark.asyncio
    async def test_top_level_fields_null_when_no_event(self, tmp_path: Path) -> None:
        """When no ``event_id`` is provided, the three per-event
        diagnostics fields must be absent or ``None`` at the top level."""
        from medre.core.storage.sqlite.storage import SQLiteStorage
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        db = tmp_path / "state.sqlite"
        storage = SQLiteStorage(db_path=str(db))
        await storage.initialize()
        await storage.close()

        bundle = await collect_evidence_bundle(storage_path=str(db))

        # The fields may be absent or None when no event is in scope.
        # The schema treats both as valid (the fields are optional).
        for key in (
            "convergence_summary",
            "orphan_report",
            "lifecycle_convergence_report",
        ):
            value = bundle.get(key)
            assert (
                value is None
            ), f"{key} must be None or absent at top level when no event_id, got {value!r}"
