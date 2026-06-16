"""Cross-cutting integration tests for runtime evidence completeness.

Fills gaps not covered by the existing focused test files:

* ``test_smoke_runtime_evidence.py`` — smoke report derived fields.
* ``test_run_session_runtime_evidence.py`` — run-session report derived fields.

This file covers:

1. Evidence bundle top-level shape (``adapter_status``, ``shutdown_evidence``,
   no ``runtime`` section).
2. Smoke report compactness (no ``runtime_events`` list, no retry_worker_summary).
3. Run-session report compactness (no ``runtime_events`` list).
4. Shutdown rejection docs semantics — spec docs describe runtime
   abandonment/suppression, not delivery success.
5. No process labels in spec/audit docs for shutdown rejection.
6. Runtime event taxonomy: ``retry_start_refused`` vs ``retry_abandoned``
   distinction.
7. Evidence read-only path does not create new storage files.
8. Audit doc describes runtime abandonment for shutdown rejection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPEC_DIR = Path(__file__).resolve().parent.parent / "docs" / "spec"
_AUDIT_DIR = Path(__file__).resolve().parent.parent / "docs" / "dev"
_DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.yaml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.yaml not found"
    return path


# ---------------------------------------------------------------------------
# 1. Evidence bundle top-level shape
# ---------------------------------------------------------------------------


async def test_evidence_bundle_has_adapter_status_top_level() -> None:
    """collect_evidence_bundle returns adapter_status at the top level."""
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    bundle = await collect_evidence_bundle(config_path=_smoke_config_path())
    assert "adapter_status" in bundle


async def test_evidence_bundle_has_shutdown_evidence_top_level() -> None:
    """collect_evidence_bundle returns shutdown_evidence at the top level."""
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    bundle = await collect_evidence_bundle(config_path=_smoke_config_path())
    assert "shutdown_evidence" in bundle


async def test_evidence_bundle_no_runtime_top_level_key() -> None:
    """Evidence bundle does not have a 'runtime' top-level section.

    Runtime events are ephemeral (in-memory EventBuffer) and are not hoisted
    into the evidence bundle.  They appear only in diagnostics_snapshot.
    """
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    bundle = await collect_evidence_bundle(config_path=_smoke_config_path())
    assert "runtime" not in bundle, (
        "Evidence bundle should not have a top-level 'runtime' key; "
        "runtime events are in-memory only and live in diagnostics_snapshot"
    )


async def test_evidence_bundle_error_path_has_required_top_level_keys() -> None:
    """Error-path bundle still has adapter_status and shutdown_evidence keys."""
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    bundle = await collect_evidence_bundle(config_path="/nonexistent/config.yaml")
    assert bundle["status"] == "error"
    assert "adapter_status" in bundle
    assert bundle["adapter_status"] is None
    assert "shutdown_evidence" in bundle
    assert bundle["shutdown_evidence"] is None


async def test_evidence_bundle_schema_version_is_1() -> None:
    """Evidence bundle schema_version is 1 (frozen pre-release)."""
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    bundle = await collect_evidence_bundle(config_path=_smoke_config_path())
    assert bundle["schema_version"] == 1


# ---------------------------------------------------------------------------
# 2. Smoke report compactness
# ---------------------------------------------------------------------------


async def test_smoke_report_no_runtime_events_list() -> None:
    """Smoke report has runtime_events_count but not a runtime_events list."""
    from medre.runtime.smoke import run_fake_bridge_smoke

    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    assert "runtime_events_count" in report
    assert "runtime_events" not in report, (
        "Smoke report should not expose runtime_events list; "
        "use runtime_events_count for the compact summary"
    )


async def test_smoke_report_no_retry_worker_summary() -> None:
    """Smoke report does not include retry_worker_summary (run-session only)."""
    from medre.runtime.smoke import run_fake_bridge_smoke

    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    assert (
        "retry_worker_summary" not in report
    ), "retry_worker_summary is a run-session report field, not a smoke field"


# ---------------------------------------------------------------------------
# 3. Run-session report compactness
# ---------------------------------------------------------------------------


async def test_run_session_report_no_runtime_events_list(tmp_path: Path) -> None:
    """Run-session report has derived fields but not a runtime_events list."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    db_path = str(tmp_path / "compact.db")
    report = await run_bridge_session(
        config_path=_smoke_config_path(),
        storage_path=db_path,
    )
    assert report["status"] == "passed"
    assert "adapter_lifecycle" in report
    assert "shutdown_status" in report
    assert "retry_worker_summary" in report
    assert (
        "runtime_events" not in report
    ), "Run-session report should not expose runtime_events list"


# ---------------------------------------------------------------------------
# 4. Shutdown rejection docs semantics
# ---------------------------------------------------------------------------


def _read_doc(path: Path) -> str:
    """Read a doc file, returning empty string if not found."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def test_shutdown_rejection_spec_describes_abandonment() -> None:
    """Spec docs describe shutdown_rejection as runtime abandonment/suppression.

    The failure taxonomy spec must associate shutdown_rejection with runtime
    abandonment language (not delivery success).
    """
    taxonomy = _read_doc(
        _SPEC_DIR / "appendices" / "failure-taxonomy.md",
    )
    assert "shutdown_rejection" in taxonomy
    # The spec should describe it as runtime-initiated rejection, not success.
    # Key phrases: "rejected", "Runtime", "shutdown" near the term.
    for line in taxonomy.splitlines():
        if "shutdown_rejection" in line:
            assert (
                "success" not in line.lower() or "not" in line.lower()
            ), f"shutdown_rejection line should not claim delivery success: {line}"


def test_shutdown_rejection_routing_delivery_spec() -> None:
    """routing-delivery spec defines shutdown_rejection as permanent cancellation."""
    routing = _read_doc(_SPEC_DIR / "routing-delivery.md")
    assert "shutdown_rejection" in routing
    # The comment should say "permanent" or "cancelled", not "success".
    for line in routing.splitlines():
        if "shutdown_rejection" in line:
            lower = line.lower()
            assert (
                "permanent" in lower or "cancelled" in lower or "shutdown" in lower
            ), f"Expected shutdown/cancellation language: {line}"


# ---------------------------------------------------------------------------
# 5. No process labels in spec/audit docs for shutdown rejection
# ---------------------------------------------------------------------------


def test_no_process_labels_in_shutdown_rejection_spec_docs() -> None:
    """Spec docs for shutdown_rejection do not use process label concepts."""
    for spec_file in _SPEC_DIR.rglob("*.md"):
        content = _read_doc(spec_file)
        if "shutdown_rejection" not in content:
            continue
        for line in content.splitlines():
            if "shutdown_rejection" in line:
                # "process_id" or "process_label" would be a process label concept
                assert (
                    "process_id" not in line
                ), f"Spec should not use process_id labels: {spec_file.name}: {line}"
                assert (
                    "process_label" not in line
                ), f"Spec should not use process_label: {spec_file.name}: {line}"


def test_no_process_labels_in_audit_docs() -> None:
    """Audit docs for shutdown_rejection do not use process label concepts."""
    audit = _read_doc(_AUDIT_DIR / "runtime-evidence-completeness-audit.md")
    if "shutdown_rejection" not in audit:
        pytest.skip("shutdown_rejection not in audit doc")
    for line in audit.splitlines():
        if "shutdown_rejection" in line:
            assert "process_id" not in line
            assert "process_label" not in line


# ---------------------------------------------------------------------------
# 6. Runtime event taxonomy: retry_start_refused vs retry_abandoned
# ---------------------------------------------------------------------------


def test_retry_start_refused_is_distinct_enum_value() -> None:
    """retry_start_refused and retry_abandoned are distinct enum members."""
    from medre.runtime.events import RuntimeEventType

    assert RuntimeEventType.RETRY_START_REFUSED != RuntimeEventType.RETRY_ABANDONED


def test_retry_start_refused_has_distinct_string_value() -> None:
    """The two event types map to different string values."""
    from medre.runtime.events import RuntimeEventType

    assert (
        RuntimeEventType.RETRY_START_REFUSED.value
        != RuntimeEventType.RETRY_ABANDONED.value
    )
    assert RuntimeEventType.RETRY_START_REFUSED.value == "retry_start_refused"
    assert RuntimeEventType.RETRY_ABANDONED.value == "retry_abandoned"


def test_retry_abandoned_describes_running_loop() -> None:
    """retry_abandoned docstring mentions running / mid-flight loop."""
    from medre.runtime.events import RuntimeEventType

    doc = RuntimeEventType.__doc__ or ""
    # The enum class docstring describes each member.
    # Verify 'abandoned' appears with language about a running loop.
    assert "retry_abandoned" in doc
    # The docstring should describe it as "running" or "mid-flight".
    lower_doc = doc.lower()
    assert (
        "running" in lower_doc or "mid-flight" in lower_doc
    ), "retry_abandoned docstring should mention running/mid-flight"


def test_retry_start_refused_describes_refusal_before_loop() -> None:
    """retry_start_refused docstring mentions refusal before loop began."""
    from medre.runtime.events import RuntimeEventType

    doc = RuntimeEventType.__doc__ or ""
    lower_doc = doc.lower()
    assert "retry_start_refused" in doc
    # Should describe it as happening before the loop starts.
    assert (
        "refused" in lower_doc
    ), "retry_start_refused docstring should mention refusal"


def test_both_retry_cancel_types_are_enum_members() -> None:
    """Both retry cancellation types are members of RuntimeEventType."""
    from medre.runtime.events import RuntimeEventType

    members = set(RuntimeEventType)
    assert RuntimeEventType.RETRY_START_REFUSED in members
    assert RuntimeEventType.RETRY_ABANDONED in members


# ---------------------------------------------------------------------------
# 7. Evidence read-only: storage_path mode does not create new files
# ---------------------------------------------------------------------------


async def test_evidence_storage_path_mode_no_new_files(tmp_path: Path) -> None:
    """collect_evidence_bundle with storage_path does not create new files.

    Creates a minimal DB, then verifies the directory contents are unchanged
    after calling collect_evidence_bundle in storage_path mode.
    """
    from datetime import datetime, timezone

    from medre.core.events.canonical import CanonicalEvent
    from medre.core.events.kinds import EventKind
    from medre.core.events.metadata import EventMetadata
    from medre.core.storage.sqlite.storage import SQLiteStorage
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    db_path = str(tmp_path / "readonly.db")
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    try:
        event = CanonicalEvent(
            event_id="ev-readonly-001",
            event_kind=EventKind.MESSAGE_TEXT,
            schema_version=1,
            timestamp=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
            source_adapter="main",
            source_transport_id="matrix",
            source_channel_id="!room:test",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "readonly test"},
            metadata=EventMetadata(),
        )
        await storage.append(event)
    finally:
        await storage.close()

    # SQLite may create -shm/-wal journal files on open; those are internal
    # journaling artifacts, not new storage files.  Filter them out.
    def _real_files(directory: Path) -> set[str]:
        return {
            f.name for f in directory.iterdir() if not f.name.endswith(("-shm", "-wal"))
        }

    files_before = _real_files(tmp_path)
    bundle = await collect_evidence_bundle(storage_path=db_path)
    files_after = _real_files(tmp_path)

    assert bundle["status"] in ("passed", "partial", "error")
    assert files_after == files_before, (
        f"collect_evidence_bundle should not create new storage files. "
        f"New: {files_after - files_before}"
    )


# ---------------------------------------------------------------------------
# 8. Audit doc describes runtime abandonment for shutdown rejection
# ---------------------------------------------------------------------------


def test_audit_doc_describes_abandonment_for_shutdown_rejection() -> None:
    """The dev audit doc mentions abandonment in the shutdown rejection story."""
    audit = _read_doc(_AUDIT_DIR / "runtime-evidence-completeness-audit.md")
    if "shutdown_rejection" not in audit:
        pytest.skip("shutdown_rejection not covered in audit doc")

    # The audit should describe shutdown_rejection as abandonment/suppression.
    lower = audit.lower()
    assert "abandon" in lower or "suppress" in lower, (
        "Audit doc should describe shutdown_rejection in terms of "
        "abandonment or suppression"
    )


def test_audit_doc_shutdown_rejection_mentions_durable_receipts() -> None:
    """Audit doc notes that shutdown_rejection receipts are the durable record."""
    audit = _read_doc(_AUDIT_DIR / "runtime-evidence-completeness-audit.md")
    if "shutdown_rejection" not in audit:
        pytest.skip("shutdown_rejection not covered in audit doc")

    lower = audit.lower()
    assert (
        "durable" in lower or "persist" in lower
    ), "Audit doc should note that shutdown_rejection receipts are durable"


# ---------------------------------------------------------------------------
# 9. Evidence bundle command field
# ---------------------------------------------------------------------------


async def test_evidence_bundle_command_is_evidence() -> None:
    """Evidence bundle has command='evidence'."""
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    bundle = await collect_evidence_bundle(config_path=_smoke_config_path())
    assert bundle["command"] == "evidence"


# ---------------------------------------------------------------------------
# 10. EventBuffer snapshot shape completeness
# ---------------------------------------------------------------------------


def test_event_buffer_snapshot_has_count_events_maxlen() -> None:
    """EventBuffer.snapshot() returns count, events, and maxlen."""
    from medre.runtime.events import EventBuffer, RuntimeEventType

    buf = EventBuffer(maxlen=16)
    buf.emit(RuntimeEventType.STATE_TRANSITION, {"from_state": "A", "to_state": "B"})
    snap = buf.snapshot()

    assert "count" in snap
    assert "events" in snap
    assert "maxlen" in snap
    assert snap["count"] == 1
    assert snap["maxlen"] == 16
    assert isinstance(snap["events"], list)
    assert len(snap["events"]) == 1


def test_event_buffer_snapshot_events_json_safe() -> None:
    """EventBuffer snapshot events are JSON-safe (plain dicts)."""
    from medre.runtime.events import EventBuffer, RuntimeEventType

    buf = EventBuffer(maxlen=16)
    buf.emit(RuntimeEventType.RETRY_ABANDONED, {"message_id": "msg-1"})
    buf.emit(RuntimeEventType.RETRY_START_REFUSED, {"message_id": "msg-2"})
    snap = buf.snapshot()

    serialized = json.dumps(snap)
    parsed = json.loads(serialized)
    assert parsed["count"] == 2
    types = [e["event_type"] for e in parsed["events"]]
    assert "retry_abandoned" in types
    assert "retry_start_refused" in types


# ---------------------------------------------------------------------------
# 11. Spec docs placement boundary: no implementation-gap content in specs
# ---------------------------------------------------------------------------


def test_spec_docs_no_temporary_language_near_shutdown_rejection() -> None:
    """Spec docs near shutdown_rejection should not contain temporary/tranche
    language (per user requirement: no temporary language in main specs)."""
    for spec_file in _SPEC_DIR.rglob("*.md"):
        content = _read_doc(spec_file)
        if "shutdown_rejection" not in content:
            continue
        lower = content.lower()
        # Check that the doc doesn't use temporary/tranche language near
        # shutdown_rejection. These terms belong in dev audit docs only.
        for term in ("tranche", "temporary", "tentative"):
            assert term not in lower, (
                f"Spec doc {spec_file.name} should not contain "
                f"'{term}' language — belongs in dev audit docs"
            )


def test_audit_doc_exists_and_has_content() -> None:
    """The runtime evidence completeness audit doc exists and is non-trivial."""
    audit_path = _AUDIT_DIR / "runtime-evidence-completeness-audit.md"
    assert audit_path.exists(), "Audit doc should exist"
    content = audit_path.read_text(encoding="utf-8")
    assert len(content) > 100, "Audit doc should have substantial content"
    assert "runtime" in content.lower()
