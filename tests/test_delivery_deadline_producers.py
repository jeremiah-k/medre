"""Delivery deadline and hard content ceiling producers.

Covers the ``content_ceiling`` configuration surface (model, loader, env
overrides, JSON schema), the admission transaction that writes the policy
ceiling into both content rows, the outbox adoption fence that enforces the
earlier-of effective deadline, and the runtime producer that derives each
admission's absolute UTC ceiling. With the section absent or disabled every
touched path behaves exactly as before the ceiling existed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from medre.config.env import (
    MedreEnvConfig,
    apply_content_ceiling_overrides,
    apply_env_overrides,
)
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import (
    ContentCeilingConfig,
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.core.storage.backend import (
    ContentCeilingMismatchError,
    OutboxDispatchSnapshotWrite,
)
from medre.core.storage.sqlite._content_identity import insert_outbox_dependency
from medre.core.storage.sqlite.connection import sync_admit_ingress
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import make_outbox_item

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CEILING_MAX_AGE_SECONDS = 31_536_000

_MINIMAL_YAML = "adapters: {}\nroutes: {}\n"


def _load_yaml(tmp_path: Path, body: str) -> RuntimeConfig:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(body, encoding="utf-8")
    config, _source, _paths = load_config(str(config_file))
    return config


# ---------------------------------------------------------------------------
# ContentCeilingConfig — strict declaration
# ---------------------------------------------------------------------------


def test_ceiling_defaults_are_disabled() -> None:
    declaration = ContentCeilingConfig().validate()
    assert declaration.enabled is False
    assert declaration.max_age_seconds is None


def test_ceiling_parses_enabled_declaration() -> None:
    declaration = ContentCeilingConfig(enabled=True, max_age_seconds=3600).validate()
    assert declaration.enabled is True
    assert declaration.max_age_seconds == 3600


def test_ceiling_enabled_boolean_is_typed() -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.enabled"):
        ContentCeilingConfig(
            enabled="true", max_age_seconds=3600  # type: ignore[arg-type]
        ).validate()


def test_ceiling_window_rejects_boolean() -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.max_age_seconds"):
        ContentCeilingConfig(
            enabled=True, max_age_seconds=True  # type: ignore[arg-type]
        ).validate()


def test_ceiling_window_rejects_string() -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.max_age_seconds"):
        ContentCeilingConfig(
            enabled=True, max_age_seconds="3600"  # type: ignore[arg-type]
        ).validate()


def test_ceiling_window_rejects_zero_and_negative() -> None:
    for seconds in (0, -1):
        with pytest.raises(ConfigValidationError, match="between 1 and"):
            ContentCeilingConfig(enabled=True, max_age_seconds=seconds).validate()


def test_ceiling_window_rejects_unbounded_declaration() -> None:
    with pytest.raises(ConfigValidationError, match="max_age_seconds"):
        ContentCeilingConfig(
            enabled=True, max_age_seconds=_CEILING_MAX_AGE_SECONDS + 1
        ).validate()


def test_ceiling_enabled_without_window_is_refused() -> None:
    with pytest.raises(ConfigValidationError, match="requires"):
        ContentCeilingConfig(enabled=True).validate()


def test_ceiling_window_may_be_declared_while_disabled() -> None:
    declaration = ContentCeilingConfig(enabled=False, max_age_seconds=3600).validate()
    assert declaration.enabled is False


# ---------------------------------------------------------------------------
# Loader — [content_ceiling] section
# ---------------------------------------------------------------------------


def test_loader_absent_section_is_disabled(tmp_path: Path) -> None:
    config = _load_yaml(tmp_path, _MINIMAL_YAML)
    assert config.content_ceiling == ContentCeilingConfig().validate()


def test_loader_section_parses_and_validates(tmp_path: Path) -> None:
    config = _load_yaml(
        tmp_path,
        "content_ceiling:\n  enabled: true\n  max_age_seconds: 900\n"
        + _MINIMAL_YAML,
    )
    assert config.content_ceiling.enabled is True
    assert config.content_ceiling.max_age_seconds == 900


def test_loader_disabled_section_with_window_loads(tmp_path: Path) -> None:
    config = _load_yaml(
        tmp_path,
        "content_ceiling:\n  enabled: false\n  max_age_seconds: 900\n"
        + _MINIMAL_YAML,
    )
    assert config.content_ceiling.enabled is False


def test_loader_unknown_section_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="unknown key"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n  window: 5\n" + _MINIMAL_YAML,
        )


def test_loader_non_boolean_enabled_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.enabled"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n" + _MINIMAL_YAML,
        )


def test_loader_enabled_without_window_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.enabled=true"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n  max_age_seconds: null\n"
            + _MINIMAL_YAML,
        )


def test_loader_window_out_of_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="between 1 and"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n  max_age_seconds: 0\n"
            + _MINIMAL_YAML,
        )


# ---------------------------------------------------------------------------
# Environment overrides — MEDRE_CONTENT_CEILING__<FIELD>
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between env-override tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_base_config() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="memory"),
    )


def test_ceiling_fields_parse_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__MAX_AGE_SECONDS", "7200")
    env = MedreEnvConfig.from_environ()
    assert env.content_ceiling_overrides["enabled"] == "true"
    assert env.content_ceiling_overrides["max_age_seconds"] == "7200"


def test_ceiling_field_names_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__enabled", "true")
    env = MedreEnvConfig.from_environ()
    assert env.content_ceiling_overrides["enabled"] == "true"


def test_ceiling_unsupported_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Unsupported MEDRE_CONTENT_CEILING__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_CEILING__WINDOW": "x"})


def test_ceiling_malformed_empty_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Malformed MEDRE_CONTENT_CEILING__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_CEILING__": "v"})


def test_ceiling_duplicate_normalized_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Duplicate normalized content_ceiling"
    ):
        MedreEnvConfig.from_environ(
            {
                "MEDRE_CONTENT_CEILING__ENABLED": "true",
                "MEDRE_CONTENT_CEILING__enabled": "false",
            }
        )


def test_ceiling_overrides_apply_and_coerce(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__MAX_AGE_SECONDS", "600")
    result = apply_env_overrides(base)
    assert result.content_ceiling.enabled is True
    assert result.content_ceiling.max_age_seconds == 600


def test_ceiling_override_enabled_without_window_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    with pytest.raises(ConfigValidationError, match="requires"):
        apply_env_overrides(base)


def test_ceiling_override_bad_window_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__MAX_AGE_SECONDS", "not-a-number")
    with pytest.raises(ConfigValidationError, match="MAX_AGE_SECONDS"):
        apply_env_overrides(base)


def test_ceiling_direct_apply_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_base_config()
    result = apply_content_ceiling_overrides(
        base, {"enabled": "true", "max_age_seconds": "60"}
    )
    assert result.content_ceiling == ContentCeilingConfig(
        enabled=True, max_age_seconds=60
    )


def test_ceiling_empty_overrides_return_same_config() -> None:
    base = _make_base_config()
    assert apply_content_ceiling_overrides(base, {}) is base


# ---------------------------------------------------------------------------
# JSON schema — docs/schemas/runtime-config.schema.json parity
# ---------------------------------------------------------------------------


def _runtime_config_schema() -> dict[str, object]:
    import json

    schema_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "schemas"
        / "runtime-config.schema.json"
    )
    schema: dict[str, object] = json.loads(
        schema_path.read_text(encoding="utf-8")
    )
    return schema


def test_schema_declares_ceiling_section_with_closed_keys() -> None:
    schema = _runtime_config_schema()
    ceiling = schema["properties"]["content_ceiling"]  # type: ignore[index]
    assert ceiling["additionalProperties"] is False  # type: ignore[index]
    assert set(ceiling["properties"]) == {  # type: ignore[index]
        "enabled",
        "max_age_seconds",
    }


def test_schema_rejects_enabled_ceiling_without_window() -> None:
    schema = _runtime_config_schema()
    validator = Draft202012Validator(schema)
    document = {"content_ceiling": {"enabled": True}}
    assert list(validator.iter_errors(document)) != []


def test_schema_accepts_disabled_and_enabled_declarations() -> None:
    validator = Draft202012Validator(_runtime_config_schema())
    assert list(validator.iter_errors({})) == []
    assert (
        list(validator.iter_errors({"content_ceiling": {"enabled": False}})) == []
    )
    assert (
        list(
            validator.iter_errors(
                {"content_ceiling": {"enabled": True, "max_age_seconds": 3600}}
            )
        )
        == []
    )


def test_schema_rejects_out_of_range_window() -> None:
    validator = Draft202012Validator(_runtime_config_schema())
    for seconds in (0, _CEILING_MAX_AGE_SECONDS + 1):
        document = {"content_ceiling": {"enabled": True, "max_age_seconds": seconds}}
        assert list(validator.iter_errors(document)) != []


# ---------------------------------------------------------------------------
# Default-off parity of the loaded model
# ---------------------------------------------------------------------------


def test_default_runtime_config_carries_disabled_ceiling() -> None:
    config = RuntimeConfig()
    assert config.content_ceiling.enabled is False
    assert config.content_ceiling.max_age_seconds is None


def test_ceiling_window_bound_matches_documented_constant() -> None:
    from medre.config.model import CONTENT_CEILING_MAX_AGE_SECONDS

    assert CONTENT_CEILING_MAX_AGE_SECONDS == 31_536_000


def test_ceiling_admission_time_derivation_shape() -> None:
    """The declared window is seconds added to a trusted admission time."""
    declared = ContentCeilingConfig(enabled=True, max_age_seconds=90).validate()
    admitted = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert admitted + timedelta(seconds=declared.max_age_seconds) == datetime(
        2026, 5, 1, 12, 1, 30, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# Storage — admission installs the ceiling into both content rows
# ---------------------------------------------------------------------------

_WINDOW_SECONDS = 3600


def _future_deadline(seconds: int = _WINDOW_SECONDS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _direct_snapshot(deadline: str | None) -> OutboxDispatchSnapshotWrite:
    """Freeze the reduced direct dispatch context exactly as staging builds it."""
    return OutboxDispatchSnapshotWrite(
        kind="direct",
        context={
            "delivery_strategy": "direct",
            "capability_level": "native",
            "capability_field": None,
            "capability_reason": None,
            "deadline": deadline,
        },
    )


async def _stage(
    store: SQLiteStorage,
    event_id: str,
    *,
    plan_deadline: str | None = None,
) -> str:
    """Create one direct outbox generation and return its outbox dependency."""
    item = make_outbox_item(event_id=event_id)
    await store.create_outbox_item(
        item, dispatch_snapshot=_direct_snapshot(plan_deadline)
    )
    rows = await store.list_mandatory_content_dependencies(event_id)
    outbox = [row for row in rows if row[0] == "outbox"]
    assert len(outbox) <= 1
    return outbox[0][2] if outbox else "absent"


async def test_admission_deadline_installs_matching_ceiling_row(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "ceiling.db"))
    await store.initialize()
    try:
        admitted_at = datetime.now(timezone.utc)
        deadline = admitted_at + timedelta(seconds=_WINDOW_SECONDS)
        await store.admit_ingress(
            make_storage_event(event_id="ceiling"), None, "live",
            content_deadline=deadline,
        )
        row = await store._read_one(
            "SELECT version, hard_ceiling, allowed_consumers, "
            "max_optional_hold_microseconds FROM event_content_access_policies "
            "WHERE event_id='ceiling'"
        )
        assert row is not None
        assert row["version"] == 1
        assert row["hard_ceiling"] == deadline.isoformat(timespec="microseconds")
        assert row["allowed_consumers"] == "[]"
        # The installed optional-hold bound spans the admitted window, minus
        # the microseconds between the test's sampling and storage's own.
        window_us = (deadline - admitted_at) // timedelta(microseconds=1)
        assert 0 < row["max_optional_hold_microseconds"] <= window_us
        dependencies = await store.list_mandatory_content_dependencies("ceiling")
        assert dependencies == (
            (
                "ingress",
                "ceiling",
                deadline.isoformat(timespec="microseconds"),
                False,
            ),
        )
    finally:
        await store.close()


async def test_admission_without_deadline_writes_no_ceiling_row(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "dormant.db"))
    await store.initialize()
    try:
        await store.admit_ingress(make_storage_event(event_id="dormant"), None, "live")
        dependencies = await store.list_mandatory_content_dependencies("dormant")
        assert dependencies == (("ingress", "dormant", None, False),)
        row = await store._read_one(
            "SELECT 1 FROM event_content_access_policies WHERE event_id='dormant'"
        )
        assert row is None
    finally:
        await store.close()


async def test_duplicate_deadline_admission_keeps_one_ceiling_row(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "dup.db"))
    await store.initialize()
    try:
        deadline = _future_deadline()
        first = await store.admit_ingress(
            make_storage_event(event_id="dup"), None, "live",
            content_deadline=deadline,
        )
        again = await store.admit_ingress(
            make_storage_event(event_id="dup"), None, "live",
            content_deadline=deadline + timedelta(seconds=30),
        )
        assert first.created and not again.created
        rows = await store._read_all(
            "SELECT hard_ceiling FROM event_content_access_policies "
            "WHERE event_id='dup'"
        )
        assert len(rows) == 1
        assert rows[0]["hard_ceiling"] == deadline.isoformat(timespec="microseconds")
    finally:
        await store.close()


async def test_mismatched_installed_ceiling_fails_closed(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "mismatch.db"))
    await store.initialize()
    try:
        # The write path validates against a pre-installed policy row before
        # any event op runs. No production route installs a ceiling row for a
        # not-yet-admitted event (the row references the canonical event), so
        # this qualification manufactures that precondition directly: the
        # seeded row stands in for an out-of-band policy installation, and
        # the admission must refuse it instead of writing a second limit.
        installed = _future_deadline(seconds=120).isoformat(timespec="microseconds")
        await store._write("PRAGMA foreign_keys=OFF", ())
        await store._write(
            "INSERT INTO event_content_access_policies "
            "(event_id, version, hard_ceiling, allowed_consumers, "
            "max_optional_hold_microseconds, created_at) "
            "VALUES ('mismatch', 1, ?, '[]', 60000000, ?)",
            (
                installed,
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            ),
        )
        await store._write("PRAGMA foreign_keys=ON", ())
        event = make_storage_event(event_id="mismatch")
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        conflicting = _future_deadline(seconds=3600).isoformat(timespec="microseconds")
        with pytest.raises(ContentCeilingMismatchError):
            await store._run_in_thread(
                sync_admit_ingress,
                store._require_db(),
                store._lock,
                event_ops=store._event_admission_ops(
                    event, recorded_at=now, content_deadline=None
                ),
                native_identity=None,
                native_insert=None,
                event_id="mismatch",
                provenance="live",
                work_status="pending",
                now_iso=now,
                content_ceiling=(conflicting, 3_600_000_000),
            )
        # The refusal rolls the whole admission back: no event, no work
        # marker, and the installed ceiling is untouched.
        event_row = await store._read_one(
            "SELECT 1 FROM canonical_events WHERE event_id='mismatch'"
        )
        assert event_row is None
        work_row = await store._read_one(
            "SELECT 1 FROM durable_ingress_work WHERE event_id='mismatch'"
        )
        assert work_row is None
        policies = await store._read_all(
            "SELECT hard_ceiling FROM event_content_access_policies "
            "WHERE event_id='mismatch'"
        )
        assert [row["hard_ceiling"] for row in policies] == [installed]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Storage — the adoption fence enforces the earlier-of effective deadline
# ---------------------------------------------------------------------------


async def test_staging_without_ceiling_or_plan_adopts_null_deadline(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "adopt-null.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="adopt-null"), None, "live"
        )
        deadline = await _stage(store, "adopt-null", plan_deadline=None)
        assert deadline is None
    finally:
        await store.close()


async def test_staging_under_ceiling_copies_the_ceiling(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "adopt-ceiling.db"))
    await store.initialize()
    try:
        ceiling = _future_deadline()
        await store.admit_ingress(
            make_storage_event(event_id="adopt-ceiling"), None, "live",
            content_deadline=ceiling,
        )
        adopted = await _stage(store, "adopt-ceiling", plan_deadline=None)
        assert adopted == ceiling.isoformat(timespec="microseconds")
    finally:
        await store.close()


async def test_staging_enforces_the_earlier_plan_deadline(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "adopt-plan-early.db"))
    await store.initialize()
    try:
        ceiling = _future_deadline(seconds=600)
        await store.admit_ingress(
            make_storage_event(event_id="plan-early"), None, "live",
            content_deadline=ceiling,
        )
        plan = datetime.now(timezone.utc) + timedelta(seconds=60)
        deadline = await _stage(store, "plan-early", plan_deadline=plan.isoformat())
        assert deadline == plan.isoformat(timespec="microseconds")
    finally:
        await store.close()


async def test_staging_ceiling_binds_a_later_plan_deadline(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "adopt-plan-late.db"))
    await store.initialize()
    try:
        ceiling = _future_deadline(seconds=60)
        await store.admit_ingress(
            make_storage_event(event_id="plan-late"), None, "live",
            content_deadline=ceiling,
        )
        plan = datetime.now(timezone.utc) + timedelta(seconds=3600)
        deadline = await _stage(store, "plan-late", plan_deadline=plan.isoformat())
        assert deadline == ceiling.isoformat(timespec="microseconds")
    finally:
        await store.close()


async def test_staging_after_the_frozen_plan_deadline_adopts_nothing(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "adopt-plan-elapsed.db"))
    await store.initialize()
    try:
        ceiling = _future_deadline(seconds=600)
        await store.admit_ingress(
            make_storage_event(event_id="plan-elapsed"), None, "live",
            content_deadline=ceiling,
        )
        elapsed_plan = datetime.now(timezone.utc) - timedelta(seconds=60)
        outcome = await _stage(
            store, "plan-elapsed", plan_deadline=elapsed_plan.isoformat()
        )
        assert outcome == "absent"
        # The generation remains an auditable obligation without content
        # authority, and the ceiling-governed source stays unreleased.
        dependencies = await store.list_mandatory_content_dependencies("plan-elapsed")
        assert [row[0] for row in dependencies] == ["ingress"]
        assert dependencies[0][3] is False
    finally:
        await store.close()


async def test_staging_after_the_ceiling_releases_and_refuses(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "adopt-ceiling-elapsed.db"))
    await store.initialize()
    try:
        deadline = _future_deadline(seconds=600)
        await store.admit_ingress(
            make_storage_event(event_id="ceiling-elapsed"), None, "live",
            content_deadline=deadline,
        )
        later = (datetime.now(timezone.utc) + timedelta(seconds=1200)).isoformat(
            timespec="microseconds"
        )
        await store._run_in_thread(
            insert_outbox_dependency,
            store._require_db(),
            "obox-elapsed",
            "ceiling-elapsed",
            later,
        )
        dependencies = await store.list_mandatory_content_dependencies(
            "ceiling-elapsed"
        )
        assert [row[0] for row in dependencies] == ["ingress"]
        assert dependencies[0][3] is True
    finally:
        await store.close()


async def test_staging_refuses_append_content_without_deadline_under_ceiling(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "append-ceiling.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="append-ceiling"))
        ceiling = _future_deadline()
        await store._write(
            "INSERT INTO event_content_access_policies "
            "(event_id, version, hard_ceiling, allowed_consumers, "
            "max_optional_hold_microseconds, created_at) "
            "VALUES ('append-ceiling', 1, ?, '[]', 3600000000, ?)",
            (ceiling.isoformat(timespec="microseconds"),) * 2,
        )
        outcome = await _stage(store, "append-ceiling", plan_deadline=None)
        assert outcome == "absent"
        dependencies = await store.list_mandatory_content_dependencies("append-ceiling")
        assert [row[0] for row in dependencies] == ["append"]
        assert dependencies[0][2] is None and dependencies[0][3] is False
    finally:
        await store.close()


async def test_staging_adopts_append_content_without_deadline_when_unceilinged(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "append-free.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="append-free"))
        outcome = await _stage(store, "append-free", plan_deadline=None)
        assert outcome is None
    finally:
        await store.close()


async def test_naive_frozen_plan_deadline_refuses_adoption(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "naive-plan.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="naive-plan"), None, "live"
        )
        naive = (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(
            tzinfo=None
        ).isoformat()
        with pytest.raises(ValueError, match="timezone-aware"):
            await _stage(store, "naive-plan", plan_deadline=naive)
        dependencies = await store.list_mandatory_content_dependencies("naive-plan")
        assert [row[0] for row in dependencies] == ["ingress"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Runtime — the producer derives each admission's absolute UTC ceiling
# ---------------------------------------------------------------------------

import dataclasses  # noqa: E402

from medre.core.storage.content_ceiling import (  # noqa: E402
    CONTENT_CEILING_MAX_AGE_SECONDS as _CORE_CEILING_BOUND,
)
from medre.core.storage.content_ceiling import ContentCeilingPolicy  # noqa: E402
from medre.core.engine.pipeline import PipelineRunner  # noqa: E402
from medre.core.routing import Router  # noqa: E402
from medre.runtime.builder import RuntimeBuilder  # noqa: E402
from tests.helpers.pipeline import (  # noqa: E402
    make_event,
    make_pipeline_config_for_pipeline,
)


def test_core_policy_derives_the_absolute_utc_ceiling() -> None:
    policy = ContentCeilingPolicy(max_age_seconds=90).validate()
    admitted = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    ceiling = policy.ceiling_for(admitted)
    assert ceiling == datetime(2026, 5, 1, 12, 1, 30, tzinfo=timezone.utc)
    assert ceiling.tzinfo is timezone.utc


def test_core_policy_refuses_naive_admission_time() -> None:
    policy = ContentCeilingPolicy(max_age_seconds=90).validate()
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.ceiling_for(datetime(2026, 5, 1, 12, 0, 0))


def test_core_policy_rejects_ill_typed_and_unbounded_windows() -> None:
    with pytest.raises(ValueError, match="integer"):
        ContentCeilingPolicy(max_age_seconds=True).validate()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 1 and"):
        ContentCeilingPolicy(max_age_seconds=0).validate()
    with pytest.raises(ValueError, match="between 1 and"):
        ContentCeilingPolicy(max_age_seconds=_CORE_CEILING_BOUND + 1).validate()


def test_core_policy_bound_matches_the_config_declaration() -> None:
    from medre.config.model import CONTENT_CEILING_MAX_AGE_SECONDS as _CONFIG_BOUND

    assert _CORE_CEILING_BOUND == _CONFIG_BOUND


def test_builder_maps_the_declaration_to_the_core_policy(tmp_path: Path) -> None:
    from medre.config.paths import MedrePaths

    paths = MedrePaths(
        config_dir=None,
        config_file=tmp_path / "config.yaml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "log",
        database_path=tmp_path / "state" / "medre.sqlite",
    )
    enabled = RuntimeConfig(
        content_ceiling=ContentCeilingConfig(enabled=True, max_age_seconds=2400)
    )
    policy = RuntimeBuilder(enabled, paths)._build_content_ceiling_policy()
    assert policy == ContentCeilingPolicy(max_age_seconds=2400)
    disabled = RuntimeConfig(
        content_ceiling=ContentCeilingConfig(enabled=False, max_age_seconds=2400)
    )
    assert RuntimeBuilder(disabled, paths)._build_content_ceiling_policy() is None
    default_builder = RuntimeBuilder(RuntimeConfig(), paths)
    assert default_builder._build_content_ceiling_policy() is None


async def test_runner_admission_produces_the_policy_deadline(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "produced.db"))
    await store.initialize()
    try:
        config = make_pipeline_config_for_pipeline(storage=store, router=Router())
        config = dataclasses.replace(
            config,
            content_ceiling=ContentCeilingPolicy(max_age_seconds=1800).validate(),
        )
        runner = PipelineRunner(config)
        before = datetime.now(timezone.utc)
        result = await runner.admit_ingress(
            make_event(event_id="produced", payload={"text": "hello"}), "live"
        )
        after = datetime.now(timezone.utc)
        assert result.created
        dependencies = await store.list_mandatory_content_dependencies("produced")
        assert len(dependencies) == 1
        owner_kind, _owner_id, deadline, released = dependencies[0]
        assert owner_kind == "ingress" and released is False
        assert deadline is not None
        produced = datetime.fromisoformat(deadline)
        assert 1799 <= (produced - before).total_seconds() <= 1801
        row = await store._read_one(
            "SELECT hard_ceiling FROM event_content_access_policies "
            "WHERE event_id='produced'"
        )
        assert row is not None
        assert row["hard_ceiling"] == deadline
    finally:
        await store.close()


async def test_runner_admission_without_a_policy_stays_dormant(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "dormant-runner.db"))
    await store.initialize()
    try:
        config = make_pipeline_config_for_pipeline(storage=store, router=Router())
        assert config.content_ceiling is None
        runner = PipelineRunner(config)
        result = await runner.admit_ingress(
            make_event(event_id="dormant-runner", payload={"text": "hello"}), "live"
        )
        assert result.created
        dependencies = await store.list_mandatory_content_dependencies(
            "dormant-runner"
        )
        assert dependencies[0][2] is None
        row = await store._read_one(
            "SELECT 1 FROM event_content_access_policies "
            "WHERE event_id='dormant-runner'"
        )
        assert row is None
    finally:
        await store.close()
