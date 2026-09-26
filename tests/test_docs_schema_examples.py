"""Schema and example validation tests.

Asserts that:

  1. Each JSON example in docs/schemas/examples/ is valid JSON.
  2. Each example validates against its corresponding schema.
  3. If jsonschema is available, perform full schema validation;
     otherwise, manually check that all required fields exist with
     correct types.
  4. Schemas have not drifted from source (basic required-field
     name check against example payloads).

Source-model property drift detection lives in
``tests/test_docs_schema_drift.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _ROOT / "docs" / "schemas"
_EXAMPLES_DIR = _SCHEMAS_DIR / "examples"

# ---------------------------------------------------------------------------
# Try importing jsonschema (optional dependency)
# ---------------------------------------------------------------------------

try:
    from importlib.util import find_spec as _find_spec

    _HAS_JSONSCHEMA = _find_spec("jsonschema") is not None
except ImportError:
    _HAS_JSONSCHEMA = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    """Load and parse a JSON file."""
    return json.loads(_read(path))


#: Mapping from example filename to its corresponding schema filename.
#: The naming convention is ``<stem>-example.json`` → ``<stem>.schema.json``.
def _example_schema_pairs() -> list[tuple[Path, Path]]:
    """Return (example_path, schema_path) pairs for all examples."""
    if not _EXAMPLES_DIR.is_dir():
        return []
    pairs: list[tuple[Path, Path]] = []
    for example in sorted(_EXAMPLES_DIR.glob("*.json")):
        # Extract the schema stem: "canonical-event-example" → "canonical-event"
        name = example.stem
        if name.endswith("-example"):
            schema_stem = name[: -len("-example")]
        else:
            schema_stem = name
        # Per-transport adapter examples validate against the shared
        # adapter-config.schema.json oneOf, not per-transport schemas.
        if schema_stem.startswith("adapter-config-"):
            schema_stem = "adapter-config"
        schema = _SCHEMAS_DIR / f"{schema_stem}.schema.json"
        if schema.exists():
            pairs.append((example, schema))
    return pairs


def _get_required_fields(schema: dict[str, Any]) -> list[str]:
    """Extract required field names from a JSON Schema object."""
    return schema.get("required", [])


def _get_property_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Extract property type declarations from a JSON Schema."""
    props = schema.get("properties", {})
    return {name: prop.get("type") for name, prop in props.items()}


def _check_type(value: Any, type_decl: Any) -> bool:
    """Check if a value matches a JSON Schema type declaration.

    Handles single types (``"string"``), union types
    (``["string", "null"]``), and None declarations (returns True).
    """
    if type_decl is None:
        return True
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    if isinstance(type_decl, list):
        return any(_check_type(value, t) for t in type_decl)
    expected = type_map.get(type_decl)
    if expected is None:
        return True
    return isinstance(value, expected)


# ===========================================================================
# 1. Examples are valid JSON
# ===========================================================================


class TestExamplesAreValidJson:
    """Each example file must parse as valid JSON."""

    @pytest.mark.parametrize(
        "example_path",
        sorted(_EXAMPLES_DIR.glob("*.json")) if _EXAMPLES_DIR.is_dir() else [],
        ids=lambda p: p.name,
    )
    def test_example_is_valid_json(self, example_path: Path) -> None:
        """Example file must parse without error."""
        try:
            _load_json(example_path)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{example_path.name}: invalid JSON: {exc}")


# ===========================================================================
# 2. Examples validate against schemas
# ===========================================================================


class TestExamplesValidateAgainstSchemas:
    """Each example must validate against its corresponding schema."""

    @pytest.mark.parametrize(
        "example_path,schema_path",
        _example_schema_pairs(),
        ids=lambda p: p.name if isinstance(p, Path) else str(p),
    )
    def test_example_validates_against_schema(
        self, example_path: Path, schema_path: Path
    ) -> None:
        """Example must satisfy all required fields from its schema."""
        example = _load_json(example_path)
        schema = _load_json(schema_path)

        if _HAS_JSONSCHEMA:
            import jsonschema

            jsonschema.validate(instance=example, schema=schema)
        else:
            # Manual validation: check required fields exist
            required = _get_required_fields(schema)
            for field in required:
                assert field in example, (
                    f"{example_path.name}: missing required field '{field}' "
                    f"from {schema_path.name}"
                )

            # Check types for present fields
            prop_types = _get_property_types(schema)
            for field, type_decl in prop_types.items():
                if field in example and type_decl is not None:
                    assert _check_type(example[field], type_decl), (
                        f"{example_path.name}: field '{field}' has type "
                        f"{type(example[field]).__name__}, expected {type_decl}"
                    )

    def test_schema_exists_for_each_example(self) -> None:
        """Every example file must have a corresponding schema file."""
        if not _EXAMPLES_DIR.is_dir():
            pytest.skip("docs/schemas/examples/ not found")
        examples = sorted(_EXAMPLES_DIR.glob("*.json"))
        for example in examples:
            name = example.stem
            schema_stem = (
                name[: -len("-example")] if name.endswith("-example") else name
            )
            # Per-transport adapter examples validate against the shared
            # adapter-config.schema.json oneOf, not per-transport schemas.
            if schema_stem.startswith("adapter-config-"):
                schema_stem = "adapter-config"
            schema = _SCHEMAS_DIR / f"{schema_stem}.schema.json"
            assert schema.exists(), (
                f"No schema found for example {example.name}. "
                f"Expected: {schema.relative_to(_ROOT)}"
            )


# ===========================================================================
# 3. Schema required fields present in examples (drift detection)
# ===========================================================================


class TestSchemaExampleFieldDrift:
    """Detect drift between schemas and examples.

    If a schema declares a required field that is absent from the example,
    or the example has a top-level field not declared in the schema, the
    test fails — indicating that schema and example have diverged.
    """

    @pytest.mark.parametrize(
        "example_path,schema_path",
        _example_schema_pairs(),
        ids=lambda p: p.name if isinstance(p, Path) else str(p),
    )
    def test_all_required_fields_present_in_example(
        self, example_path: Path, schema_path: Path
    ) -> None:
        """Every required field from the schema must appear in the example."""
        example = _load_json(example_path)
        schema = _load_json(schema_path)
        required = _get_required_fields(schema)
        missing = [f for f in required if f not in example]
        assert not missing, (
            f"{example_path.name}: missing required fields from "
            f"{schema_path.name}: {missing}"
        )

    @pytest.mark.parametrize(
        "example_path,schema_path",
        _example_schema_pairs(),
        ids=lambda p: p.name if isinstance(p, Path) else str(p),
    )
    def test_example_fields_match_schema(
        self, example_path: Path, schema_path: Path
    ) -> None:
        """Top-level example fields must be declared in the schema.

        This catches schema drift where examples have fields that the
        schema doesn't document.  Allows ``additionalProperties: true``
        schemas to pass — only flags when the schema explicitly sets
        ``additionalProperties: false``.
        """
        example = _load_json(example_path)
        schema = _load_json(schema_path)

        # Only check if schema disallows additional properties.
        if schema.get("additionalProperties", True) is not False:
            return

        schema_props = set(schema.get("properties", {}).keys())
        example_fields = set(example.keys())
        extra = example_fields - schema_props
        assert not extra, (
            f"{example_path.name}: has top-level fields not in "
            f"{schema_path.name}: {sorted(extra)}. "
            f"Either update the schema or remove the extra fields."
        )


# ===========================================================================
# 5. Evidence bundle new-field schema validation
# ===========================================================================


class TestEvidenceBundleSchemaNewFields:
    """Validate that the evidence-bundle schema accepts the new runtime fields
    (evidence_tier, adapter_status, shutdown_evidence) and rejects invalid values.
    """

    @pytest.fixture()
    def _schema(self) -> dict[str, Any]:
        return _load_json(_SCHEMAS_DIR / "evidence-bundle.schema.json")

    def _minimal_bundle(self) -> dict[str, Any]:
        """Return a minimal evidence bundle matching runtime output shape."""
        return {
            "adapter_status": None,
            "collected_at": "2026-05-27T12:00:00+00:00",
            "command": "evidence",
            "config_source": "file",
            "convergence_summary": None,
            "errors": [],
            "evidence_tier": "synthetic",
            "generated_at": "2026-05-27T12:00:02+00:00",
            "lifecycle_convergence_report": None,
            "limitations": [],
            "medre_version": "0.1.0",
            "orphan_report": None,
            "recovery_ledger": None,
            "recovery_summary": None,
            "runtime_started": False,
            "schema_version": 1,
            "sections": {},
            "shutdown_evidence": None,
            "status": "passed",
        }

    def test_minimal_bundle_with_new_fields_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """A minimal bundle with the new fields must validate."""
        bundle = self._minimal_bundle()
        if _HAS_JSONSCHEMA:
            import jsonschema

            jsonschema.validate(instance=bundle, schema=_schema)
        else:
            # Manual check: all required fields present.
            required = _get_required_fields(_schema)
            missing = [f for f in required if f not in bundle]
            assert not missing, f"Missing required fields: {missing}"

    def test_invalid_evidence_tier_rejected(self, _schema: dict[str, Any]) -> None:
        """An invalid evidence_tier value must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")

        import jsonschema

        bundle = self._minimal_bundle()
        bundle["evidence_tier"] = "invalid_tier"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_all_valid_tiers_accepted(self, _schema: dict[str, Any]) -> None:
        """Each valid evidence_tier value must pass validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")

        import jsonschema

        for tier in (
            "synthetic",
            "conformance",
            "docker",
            "live_service",
            "hardware",
        ):
            bundle = self._minimal_bundle()
            bundle["evidence_tier"] = tier
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_adapter_status_with_entries_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """adapter_status as a list of adapter status objects must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")

        import jsonschema

        bundle = self._minimal_bundle()
        bundle["adapter_status"] = [
            {
                "adapter_id": "matrix_main",
                "adapter_kind": "real",
                "configured": True,
                "connected": True,
                "current_state": "ready",
                "enabled": True,
                "failure_category": None,
                "failure_reason": None,
                "health": "healthy",
                "operator_status": "connected",
                "transport": "matrix",
                "valid_transitions": ["degraded", "disconnected", "stopping"],
            },
        ]
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_evidence_with_values_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """shutdown_evidence as a populated object must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")

        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": 0,
            "outbox_shutdown_policy": "resumable",
            "pending_outbox_counts": {},
            "pending_retry_work_total": 0,
            "resume_expected": False,
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 5,
            "retry_worker_running": False,
            "retry_worker_succeeded": 5,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
        }
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_fake_live_tier_not_overclaimed(self, _schema: dict[str, Any]) -> None:
        """Schema validation: default tier is synthetic, live_service/hardware require explicit opt-in."""
        bundle = self._minimal_bundle()
        assert bundle["evidence_tier"] == "synthetic"
        # Explicit live_service validates when set
        bundle["evidence_tier"] = "live_service"
        if _HAS_JSONSCHEMA:
            import jsonschema

            jsonschema.validate(instance=bundle, schema=_schema)
        # Explicit hardware validates when set
        bundle["evidence_tier"] = "hardware"
        if _HAS_JSONSCHEMA:
            import jsonschema

            jsonschema.validate(instance=bundle, schema=_schema)

    def test_missing_evidence_tier_fails(self, _schema: dict[str, Any]) -> None:
        """Missing evidence_tier must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        del bundle["evidence_tier"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_null_evidence_tier_fails(self, _schema: dict[str, Any]) -> None:
        """Null evidence_tier must fail validation (string type required)."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["evidence_tier"] = None
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_adapter_status_as_dict_fails(self, _schema: dict[str, Any]) -> None:
        """adapter_status as a plain dict must fail validation (expects null or array)."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["adapter_status"] = {"adapter_id": "bad"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_adapter_status_as_string_fails(self, _schema: dict[str, Any]) -> None:
        """adapter_status as a string must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["adapter_status"] = "connected"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_adapter_status_item_missing_required_field_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """A list item missing a required field must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["adapter_status"] = [
            {
                "adapter_id": "matrix_main",
                # missing operator_status, adapter_kind, configured, etc.
            },
        ]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_negative_in_flight_fails(self, _schema: dict[str, Any]) -> None:
        """Negative in_flight_count must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": -1,
            "outbox_shutdown_policy": "resumable",
            "pending_outbox_counts": None,
            "pending_retry_work_total": 0,
            "resume_expected": False,
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 0,
            "retry_worker_running": False,
            "retry_worker_succeeded": 0,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_negative_pending_outbox_count_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """Negative pending_outbox_counts value must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": 0,
            "outbox_shutdown_policy": "resumable",
            "pending_outbox_counts": {"pending": -3},
            "pending_retry_work_total": 0,
            "resume_expected": False,
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 0,
            "retry_worker_running": False,
            "retry_worker_succeeded": 0,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    # -- strictness: additionalProperties: false on new $defs ----------------

    def test_adapter_status_extra_field_rejected(self, _schema: dict[str, Any]) -> None:
        """Extra fields inside adapter_status entries must be rejected."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")

        import jsonschema

        bundle = self._minimal_bundle()
        bundle["adapter_status"] = [
            {
                "adapter_id": "matrix_main",
                "adapter_kind": "real",
                "configured": True,
                "connected": True,
                "current_state": "ready",
                "enabled": True,
                "failure_category": None,
                "failure_reason": None,
                "health": "healthy",
                "operator_status": "connected",
                "transport": "matrix",
                "valid_transitions": ["degraded", "stopping"],
                "unexpected_extra": "must_fail",
            },
        ]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_evidence_extra_field_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """Extra fields inside shutdown_evidence must be rejected."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")

        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": 0,
            "outbox_shutdown_policy": None,
            "pending_outbox_counts": None,
            "pending_retry_work_total": 0,
            "resume_expected": False,
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 0,
            "retry_worker_running": False,
            "retry_worker_succeeded": 0,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
            "rogue_field": "must_fail",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    # -- recovery ownership action / source enum validation -------------------

    def test_invalid_ownership_action_rejected_by_schema(
        self, _schema: dict[str, Any]
    ) -> None:
        """Invalid ownership_action value must be rejected by schema."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["recovery_ledger"] = {
            "actions": [
                {
                    "delivery_plan_id": "plan-1",
                    "event_id": "ev-1",
                    "outbox_id": "ob-1",
                    "ownership_action": "INVALID_ACTION",
                    "prior_status": "pending",
                    "reason": "test",
                    "observed_status": "pending",
                    "recovery_run_id": None,
                    "recovery_source": "startup_recovery",
                    "replay_run_id": None,
                    "startup_timestamp": None,
                    "timestamp": "2026-05-31T12:00:00+00:00",
                    "worker_identity": None,
                },
            ],
            "generated_at": "2026-05-31T12:00:00+00:00",
            "recovery_run_id": None,
            "startup_timestamp": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_invalid_recovery_source_rejected_by_schema(
        self, _schema: dict[str, Any]
    ) -> None:
        """Invalid recovery_source value must be rejected by schema."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["recovery_ledger"] = {
            "actions": [
                {
                    "delivery_plan_id": "plan-1",
                    "event_id": "ev-1",
                    "outbox_id": "ob-1",
                    "ownership_action": "recoverable",
                    "prior_status": "pending",
                    "reason": "test",
                    "observed_status": "pending",
                    "recovery_run_id": None,
                    "recovery_source": "INVALID_SOURCE",
                    "replay_run_id": None,
                    "startup_timestamp": None,
                    "timestamp": "2026-05-31T12:00:00+00:00",
                    "worker_identity": None,
                },
            ],
            "generated_at": "2026-05-31T12:00:00+00:00",
            "recovery_run_id": None,
            "startup_timestamp": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_evidence_resume_expected_true_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """resume_expected=True with pending work must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": 0,
            "outbox_shutdown_policy": "resumable",
            "pending_outbox_counts": {"pending": 5},
            "pending_retry_work_total": 5,
            "resume_expected": True,
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 0,
            "retry_worker_running": False,
            "retry_worker_succeeded": 0,
            "runtime_state": "stopped",
            "shutdown_reason": "shutdown_pending",
            "shutdown_status": "shutdown_pending",
            "tasks_cancelled": None,
        }
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_evidence_missing_resume_expected_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """Missing resume_expected must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": 0,
            "outbox_shutdown_policy": "resumable",
            "pending_outbox_counts": {},
            "pending_retry_work_total": 0,
            # resume_expected deliberately omitted
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 0,
            "retry_worker_running": False,
            "retry_worker_succeeded": 0,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_evidence_missing_outbox_shutdown_policy_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """Missing outbox_shutdown_policy must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": "flushed",
            "in_flight_count": 0,
            # outbox_shutdown_policy deliberately omitted
            "pending_outbox_counts": {},
            "pending_retry_work_total": 0,
            "resume_expected": False,
            "retry_worker_dead_lettered": 0,
            "retry_worker_failed": 0,
            "retry_worker_processed": 0,
            "retry_worker_running": False,
            "retry_worker_succeeded": 0,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_convergence_summary_null_validates(self, _schema: dict[str, Any]) -> None:
        """convergence_summary=null (no per-event data) must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["convergence_summary"] = None
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_convergence_summary_populated_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """convergence_summary with a populated ConvergenceSummary must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["convergence_summary"] = {
            "evidence_bundle_ref": None,
            "orphan_count": None,
            "severity_counts": {"safe": 1, "degraded": 0, "inconsistent": 0},
            "targets": [
                {
                    "event_id": "evt-1",
                    "delivery_plan_id": "plan-1",
                    "target_adapter": "matrix",
                    "target_channel": "!room:example.com",
                    "outbox_status": "sent",
                    "current_receipt_status": "sent",
                    "current_receipt_id": "rcpt-001",
                    "current_attempt_number": 1,
                    "severity": "safe",
                    "warnings": [],
                    "outbox_id": "ob-001",
                },
            ],
            "total_targets": 1,
            "warnings": [],
            "worst_severity": "safe",
        }
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_convergence_summary_missing_severity_count_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """convergence_summary missing required severity_counts must fail."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["convergence_summary"] = {
            "evidence_bundle_ref": None,
            "orphan_count": None,
            "targets": [],
            "total_targets": 0,
            "warnings": [],
            "worst_severity": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_convergence_summary_target_extra_field_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """Extra fields inside convergence_summary targets must be rejected."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["convergence_summary"] = {
            "evidence_bundle_ref": None,
            "orphan_count": None,
            "severity_counts": {"safe": 0, "degraded": 0, "inconsistent": 0},
            "targets": [
                {
                    "event_id": "evt-1",
                    "delivery_plan_id": "plan-1",
                    "target_adapter": "matrix",
                    "target_channel": None,
                    "outbox_status": None,
                    "current_receipt_status": None,
                    "current_receipt_id": None,
                    "current_attempt_number": None,
                    "severity": "safe",
                    "warnings": [],
                    "outbox_id": None,
                    "rogue_field": "must_fail",
                },
            ],
            "total_targets": 1,
            "warnings": [],
            "worst_severity": "safe",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_shutdown_evidence_null_policy_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """outbox_shutdown_policy=null (no outbox data) must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["shutdown_evidence"] = {
            "drain_timeout_detected": False,
            "evidence_flush_status": None,
            "in_flight_count": None,
            "outbox_shutdown_policy": None,
            "pending_outbox_counts": None,
            "pending_retry_work_total": None,
            "resume_expected": False,
            "retry_worker_dead_lettered": None,
            "retry_worker_failed": None,
            "retry_worker_processed": None,
            "retry_worker_running": None,
            "retry_worker_succeeded": None,
            "runtime_state": "stopped",
            "shutdown_reason": None,
            "shutdown_status": "graceful_stop",
            "tasks_cancelled": None,
        }
        jsonschema.validate(instance=bundle, schema=_schema)

    # -- orphan_report validation ---------------------------------------------

    def test_orphan_report_null_validates(self, _schema: dict[str, Any]) -> None:
        """orphan_report=null (no orphan data) must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["orphan_report"] = None
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_orphan_report_populated_validates(self, _schema: dict[str, Any]) -> None:
        """orphan_report with a populated OrphanReport must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["orphan_report"] = {
            "findings": [
                {
                    "kind": "orphaned_parent_receipt",
                    "severity": "inconsistent",
                    "record_id": "rcpt-001",
                    "record_type": "receipt",
                    "details": "Receipt references missing parent",
                    "extra": {
                        "receipt_id": "rcpt-001",
                        "parent_receipt_id": "rcpt-missing",
                    },
                },
            ],
            "total_findings": 1,
            "severity_counts": {"safe": 0, "degraded": 0, "inconsistent": 1},
            "worst_severity": "inconsistent",
            "summary": "1 finding(s): 1 inconsistent, 0 degraded",
        }
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_orphan_report_missing_required_field_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """OrphanFinding missing required field 'kind' must fail validation."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["orphan_report"] = {
            "findings": [
                {
                    "severity": "inconsistent",
                    "record_id": "rcpt-001",
                    "record_type": "receipt",
                    "details": "Missing kind field",
                    "extra": {},
                },
            ],
            "total_findings": 1,
            "severity_counts": {"safe": 0, "degraded": 0, "inconsistent": 1},
            "worst_severity": "inconsistent",
            "summary": "1 finding(s): 1 inconsistent, 0 degraded",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    # -- lifecycle_convergence_report validation --------------------------------

    def test_lifecycle_convergence_report_null_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """lifecycle_convergence_report=null (no per-event data) must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = None
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_convergence_report_populated_validates(
        self, _schema: dict[str, Any]
    ) -> None:
        """lifecycle_convergence_report with populated findings must validate."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [
                {
                    "kind": "terminal_receipt_nonterminal_outbox",
                    "severity": "degraded",
                    "record_id": "ob-001",
                    "record_type": "outbox",
                    "details": "Terminal receipt (sent) but outbox is non-terminal (pending)",
                    "extra": {
                        "outbox_id": "ob-001",
                        "receipt_id": "rcpt-001",
                        "outbox_status": "pending",
                        "receipt_status": "sent",
                    },
                },
            ],
            "total_findings": 1,
            "severity_counts": {"safe": 0, "degraded": 1, "inconsistent": 0},
            "worst_severity": "degraded",
        }
        jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_convergence_report_invalid_kind_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """Invalid lifecycle finding kind must be rejected."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [
                {
                    "kind": "orphaned_outbox",
                    "severity": "inconsistent",
                    "record_id": "ob-001",
                    "record_type": "outbox",
                    "details": "Wrong kind",
                    "extra": {},
                },
            ],
            "total_findings": 1,
            "severity_counts": {"safe": 0, "degraded": 0, "inconsistent": 1},
            "worst_severity": "inconsistent",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_convergence_report_invalid_severity_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """Invalid severity value in lifecycle finding must be rejected."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [
                {
                    "kind": "next_retry_in_past",
                    "severity": "safe",
                    "record_id": "ob-001",
                    "record_type": "outbox",
                    "details": "Bad severity",
                    "extra": {},
                },
            ],
            "total_findings": 1,
            "severity_counts": {"safe": 1, "degraded": 0, "inconsistent": 0},
            "worst_severity": "safe",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_convergence_report_finding_extra_field_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """Extra top-level fields inside lifecycle findings must be rejected."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [
                {
                    "kind": "receipt_sequence_gap",
                    "severity": "degraded",
                    "record_id": "rcpt-001",
                    "record_type": "receipt",
                    "details": "Gap detected",
                    "extra": {},
                    "rogue_field": "must_fail",
                },
            ],
            "total_findings": 1,
            "severity_counts": {"safe": 0, "degraded": 1, "inconsistent": 0},
            "worst_severity": "degraded",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_convergence_report_missing_required_fails(
        self, _schema: dict[str, Any]
    ) -> None:
        """LifecycleConvergenceReport missing required field must fail."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [],
            "total_findings": 0,
            # missing severity_counts and worst_severity
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_report_severity_counts_empty_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """severity_counts={} must be rejected (requires safe/degraded/inconsistent)."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [],
            "total_findings": 0,
            "severity_counts": {},
            "worst_severity": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_report_severity_counts_bad_key_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """severity_counts={'foo': 1} must be rejected (additionalProperties: false)."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [],
            "total_findings": 0,
            "severity_counts": {"foo": 1},
            "worst_severity": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)

    def test_lifecycle_report_severity_counts_missing_key_rejected(
        self, _schema: dict[str, Any]
    ) -> None:
        """severity_counts missing 'inconsistent' must be rejected (required)."""
        if not _HAS_JSONSCHEMA:
            pytest.skip("jsonschema not installed")
        import jsonschema

        bundle = self._minimal_bundle()
        bundle["lifecycle_convergence_report"] = {
            "findings": [],
            "total_findings": 0,
            "severity_counts": {"safe": 0, "degraded": 0},
            "worst_severity": None,
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bundle, schema=_schema)


# ===========================================================================
# 6. Per-transport adapter-config example coverage and oneOf validation
# ===========================================================================


#: Distinctive required/property fields per adapter-config oneOf arm title.
_ADAPTER_ARM_DISTINCTIVE_FIELDS: dict[str, frozenset[str]] = {
    "MatrixConfig": frozenset({"homeserver", "user_id"}),
    "MeshtasticConfig": frozenset({"radio_relay_prefix", "encrypted_action"}),
    "MeshCoreConfig": frozenset({"meshcore_relay_prefix", "serial_baudrate"}),
    "LxmfConfig": frozenset({"lxmf_relay_prefix", "default_delivery_method"}),
}

_ADAPTER_CONFIG_EXAMPLES = sorted(_EXAMPLES_DIR.glob("adapter-config*-example.json"))


class TestAdapterConfigExampleCoverage:
    """Verify per-transport example coverage (TC-008) and that each example
    validates against exactly one arm of the adapter-config oneOf.
    """

    def test_each_oneof_arm_has_example(self) -> None:
        """Each arm of adapter-config.schema.json oneOf must have at least
        one example payload in docs/schemas/examples/."""
        schema = _load_json(_SCHEMAS_DIR / "adapter-config.schema.json")
        arm_titles = [arm["title"] for arm in schema["oneOf"]]

        examples = {p.name: _load_json(p) for p in _ADAPTER_CONFIG_EXAMPLES}

        for title in arm_titles:
            assert (
                title in _ADAPTER_ARM_DISTINCTIVE_FIELDS
            ), f"Add distinctive field mapping for new adapter arm {title!r}"
            distinctive = _ADAPTER_ARM_DISTINCTIVE_FIELDS[title]
            has_example = any(distinctive <= set(ex.keys()) for ex in examples.values())
            assert has_example, (
                f"No example payload found for adapter-config oneOf arm "
                f"{title!r}. Expected an example with fields: {sorted(distinctive)}"
            )

    @pytest.mark.parametrize(
        "example_path",
        _ADAPTER_CONFIG_EXAMPLES,
        ids=lambda p: p.name,
    )
    def test_each_example_validates_against_exactly_one_arm(
        self, example_path: Path
    ) -> None:
        """Each adapter-config-*-example.json must validate against exactly
        one arm of the adapter-config.schema.json oneOf."""
        schema = _load_json(_SCHEMAS_DIR / "adapter-config.schema.json")
        example = _load_json(example_path)

        if _HAS_JSONSCHEMA:
            import jsonschema

            matches = 0
            for arm in schema["oneOf"]:
                try:
                    jsonschema.validate(instance=example, schema=arm)
                    matches += 1
                except jsonschema.ValidationError:
                    pass
            assert matches == 1, (
                f"{example_path.name}: expected to validate against exactly "
                f"1 arm, got {matches}"
            )
        else:
            # Manual: check required fields + additionalProperties: false.
            matches = 0
            for arm in schema["oneOf"]:
                required = arm.get("required", [])
                if not all(r in example for r in required):
                    continue
                props = set(arm.get("properties", {}).keys())
                if set(example.keys()) <= props:
                    matches += 1
            assert matches == 1, (
                f"{example_path.name}: expected to match exactly 1 arm "
                f"manually, got {matches}"
            )


# ===========================================================================
# 7. Structured ContextMapEntry in routing example (TC-009)
# ===========================================================================


class TestStructuredContextMapEntry:
    """Verify the structured ContextMapEntry shape (dict with a
    dest_context/dest_destination key) appears in at least one example
    payload."""

    @staticmethod
    def _example_routes() -> list[dict]:
        """Load the routing example, navigating a RouteConfigSet wrapper.

        routing-config-example.json may be a single RouteConfig object or
        a ``{"routes": [...]}`` set demonstrating several shapes.
        """
        example = _load_json(_EXAMPLES_DIR / "routing-config-example.json")
        if "routes" in example:
            return example["routes"]
        return [example]

    def test_routing_example_has_structured_entry(self) -> None:
        """routing-config-example.json must contain at least one structured
        context_map entry (a dict with a dest_context or dest_destination
        key, not a bare string)."""
        context_map = next(
            (
                r.get("context_map")
                for r in self._example_routes()
                if r.get("context_map")
            ),
            None,
        )
        assert (
            context_map is not None
        ), "routing-config-example.json missing context_map"
        assert isinstance(
            context_map, dict
        ), f"context_map must be a dict, got {type(context_map)}"

        structured_entries = [
            key
            for key, val in context_map.items()
            if isinstance(val, dict)
            and ("dest_context" in val or "dest_destination" in val)
        ]
        assert structured_entries, (
            "No structured ContextMapEntry found in "
            "routing-config-example.json context_map. "
            "Expected at least one entry shaped like "
            '{"dest_context": "...", "source_origin_label": ...} or '
            '{"dest_destination": {...}}'
        )

    def test_structured_entry_has_exactly_one_addressing_field(self) -> None:
        """Each structured entry carries dest_context XOR dest_destination."""
        context_map = next(
            (r["context_map"] for r in self._example_routes() if r.get("context_map")),
            None,
        )
        assert context_map is not None
        for key, val in context_map.items():
            assert isinstance(val, dict), (
                f"context_map[{key!r}] must be a structured table, " f"got {type(val)}"
            )
            has_context = "dest_context" in val
            has_destination = "dest_destination" in val
            assert has_context or has_destination, (
                f"context_map[{key!r}] is a dict but missing both "
                "'dest_context' and 'dest_destination'"
            )
            assert not (has_context and has_destination), (
                f"context_map[{key!r}] carries both 'dest_context' and "
                "'dest_destination' — exactly one is allowed"
            )
            if has_context:
                assert isinstance(
                    val["dest_context"], str
                ), f"context_map[{key!r}].dest_context must be a string"


# ===========================================================================
# 8. (filter_hooks removed — A8)
# ===========================================================================


def test_builtin_native_metadata_contracts_have_schema_example_pairs() -> None:
    """Every built-in native metadata contract participates in conformance."""
    expected = {
        "matrix-native-metadata",
        "meshtastic-native-metadata",
        "meshcore-native-metadata",
        "lxmf-native-metadata",
    }
    actual = {
        example.stem.removesuffix("-example")
        for example, schema in _example_schema_pairs()
        if schema.stem.removesuffix(".schema") in expected
    }
    assert actual == expected
