"""Strict operator retention policy declaration and activation epochs.

Covers the versioned policy value object, the durable epoch record, the
``content_retention`` configuration surface (model, loader, env overrides),
and the storage epoch/preview behavior introduced by the retention slice.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import msgspec
import pytest
from jsonschema import Draft202012Validator

from medre.config.env import (
    MedreEnvConfig,
    apply_content_retention_overrides,
    apply_env_overrides,
)
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config, parse_runtime_config
from medre.config.model import (
    ContentRetentionConfig,
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import resolve
from medre.core.storage.backend import ContentCollectionEpochChanged
from medre.core.storage.content_retention import (
    COLLECTION_UNMEASURED_SCOPES,
    RETENTION_PREVIEW_OUTCOMES,
    ContentRetentionPolicy,
    RetentionEpoch,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.content_retention import seed_eligible_content as _seed_eligible_at
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# ContentRetentionPolicy — strict versioned declaration
# ---------------------------------------------------------------------------


def test_policy_defaults_are_retain_and_never_collect() -> None:
    policy = ContentRetentionPolicy()
    assert policy.version == 1
    assert policy.mode == "retain"
    assert policy.interval_seconds == 300
    assert policy.batch_limit == 25
    assert policy.retained is True


def test_policy_relay_only_is_explicit_opt_in() -> None:
    policy = ContentRetentionPolicy(mode="relay-only")
    assert policy.retained is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": True},
        {"version": "1"},
        {"version": 2},
        {"version": 1.0},
        {"version": None},
        {"mode": True},
        {"mode": None},
        {"mode": "disabled"},
        {"mode": "relayonly"},
        {"mode": "RELAY-ONLY"},
        {"mode": ""},
        {"interval_seconds": True},
        {"interval_seconds": 0},
        {"interval_seconds": -1},
        {"interval_seconds": 86_401},
        {"interval_seconds": 300.0},
        {"interval_seconds": "300"},
        {"batch_limit": True},
        {"batch_limit": 0},
        {"batch_limit": 101},
        {"batch_limit": -5},
        {"batch_limit": 25.0},
    ],
)
def test_policy_refuses_invalid_declarations(overrides: dict) -> None:
    with pytest.raises(ValueError):
        ContentRetentionPolicy(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"interval_seconds": 1},
        {"interval_seconds": 86_400},
        {"batch_limit": 1},
        {"batch_limit": 100},
    ],
)
def test_policy_accepts_cadence_bounds(overrides: dict) -> None:
    ContentRetentionPolicy(**overrides)


# ---------------------------------------------------------------------------
# RetentionEpoch — durable activation record
# ---------------------------------------------------------------------------


def _epoch(**overrides: object) -> RetentionEpoch:
    values: dict[str, object] = {
        "epoch": 1,
        "mode": "relay-only",
        "activated_at": "2026-11-01T00:00:00+00:00",
        "admission_watermark": "2026-10-31T23:00:00+00:00",
        "last_observed_active_at": None,
        "activated_by": "operator",
    }
    values.update(overrides)
    return RetentionEpoch(**values)  # type: ignore[arg-type]


def test_epoch_accepts_null_watermark_for_empty_store() -> None:
    epoch = _epoch(admission_watermark=None)
    assert epoch.admission_watermark is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"epoch": True},
        {"epoch": 0},
        {"epoch": -1},
        {"epoch": "1"},
        {"mode": "retain"},
        {"mode": None},
        {"activated_at": ""},
        {"activated_at": None},
        {"admission_watermark": ""},
        {"last_observed_active_at": ""},
        {"activated_by": "scheduler"},
        {"activated_by": None},
        {"activated_by": True},
    ],
)
def test_epoch_refuses_invalid_records(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _epoch(**overrides)


def test_epoch_activator_vocabulary_is_closed() -> None:
    from medre.core.storage.content_retention import RETENTION_EPOCH_ACTIVATORS

    assert RETENTION_EPOCH_ACTIVATORS == frozenset({"runtime-first-start", "operator"})


# ---------------------------------------------------------------------------
# Preview outcome vocabulary
# ---------------------------------------------------------------------------


def test_preview_outcome_vocabulary_is_closed() -> None:
    assert RETENTION_PREVIEW_OUTCOMES == (
        "eligible",
        "blocked",
        "empty",
        "already_collected",
        "inconsistent",
    )


def test_unmeasured_scopes_are_reused_from_the_collector() -> None:
    from medre.core.storage.sqlite._collection import (
        COLLECTION_UNMEASURED_SCOPES as COLLECTOR_SCOPES,
    )

    assert COLLECTION_UNMEASURED_SCOPES is COLLECTOR_SCOPES
    assert len(COLLECTION_UNMEASURED_SCOPES) >= 1


# ---------------------------------------------------------------------------
# ContentRetentionConfig — typed config surface
# ---------------------------------------------------------------------------


def test_defaults_are_disabled() -> None:
    config = ContentRetentionConfig().validate()
    assert config.enabled is False
    assert config.version == 1
    assert config.mode == "retain"
    assert config.interval_seconds == 300
    assert config.batch_limit == 25
    assert RuntimeConfig().content_retention == config


def test_disabled_retain_is_valid() -> None:
    ContentRetentionConfig(enabled=False, mode="retain").validate()


def test_enabled_retain_is_a_declared_observing_policy() -> None:
    ContentRetentionConfig(enabled=True, mode="retain").validate()


def test_relay_only_requires_enabled() -> None:
    with pytest.raises(ConfigValidationError, match="relay-only"):
        ContentRetentionConfig(enabled=False, mode="relay-only").validate()


def test_enabled_relay_only_is_valid() -> None:
    config = ContentRetentionConfig(
        enabled=True, mode="relay-only", interval_seconds=60, batch_limit=10
    ).validate()
    assert config.mode == "relay-only"


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": "yes"},
        {"enabled": 1},
        {"version": True},
        {"version": "1"},
        {"version": 2},
        {"mode": "disabled"},
        {"mode": True},
        {"interval_seconds": True},
        {"interval_seconds": 0},
        {"interval_seconds": 86_401},
        {"batch_limit": True},
        {"batch_limit": 0},
        {"batch_limit": 101},
    ],
)
def test_invalid_fields_rejected(overrides: dict) -> None:
    with pytest.raises(ConfigValidationError):
        ContentRetentionConfig(**overrides).validate()


# ---------------------------------------------------------------------------
# Loader — YAML surface
# ---------------------------------------------------------------------------


def _load_yaml(tmp_path, body: str) -> RuntimeConfig:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(body, encoding="utf-8")
    config, _source, _paths = load_config(str(config_file))
    return config


def test_absent_section_means_disabled_defaults(tmp_path) -> None:
    config = _load_yaml(tmp_path, "adapters: {}\nroutes: {}\n")
    assert config.content_retention == ContentRetentionConfig().validate()


def test_section_parses_and_validates(tmp_path) -> None:
    config = _load_yaml(
        tmp_path,
        "content_retention:\n"
        "  enabled: true\n"
        "  version: 1\n"
        "  mode: relay-only\n"
        "  interval_seconds: 60\n"
        "  batch_limit: 10\n"
        "adapters: {}\n"
        "routes: {}\n",
    )
    assert config.content_retention.enabled is True
    assert config.content_retention.mode == "relay-only"
    assert config.content_retention.interval_seconds == 60
    assert config.content_retention.batch_limit == 10


def test_unknown_section_key_rejected(tmp_path) -> None:
    with pytest.raises(ConfigValidationError, match="content_retention"):
        _load_yaml(
            tmp_path,
            "content_retention:\n  enabled: true\n  profile: history\n"
            "adapters: {}\nroutes: {}\n",
        )


def test_typo_root_key_rejected_includes_new_section_in_accepted_keys(tmp_path) -> None:
    with pytest.raises(ConfigValidationError, match="content_retention") as exc:
        _load_yaml(
            tmp_path,
            "content_retentation:\n  enabled: true\n" "adapters: {}\nroutes: {}\n",
        )
    assert "content_retention" in str(exc.value)


def test_relay_only_without_enabled_rejected_at_load(tmp_path) -> None:
    with pytest.raises(ConfigValidationError, match="relay-only"):
        _load_yaml(
            tmp_path,
            "content_retention:\n  mode: relay-only\n" "adapters: {}\nroutes: {}\n",
        )


def test_version_boolean_rejected_at_load(tmp_path) -> None:
    with pytest.raises(ConfigValidationError, match="version"):
        _load_yaml(
            tmp_path,
            "content_retention:\n  enabled: true\n  version: true\n"
            "adapters: {}\nroutes: {}\n",
        )


def test_parse_runtime_config_is_the_authoring_interface() -> None:
    config = parse_runtime_config(
        {
            "content_retention": {"enabled": True, "mode": "relay-only"},
            "adapters": {},
            "routes": {},
        },
        resolve(),
    )
    assert config.content_retention.mode == "relay-only"


# ---------------------------------------------------------------------------
# Environment overrides — MEDRE_CONTENT_RETENTION__<FIELD>
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


def test_fields_parse_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__MODE", "relay-only")
    env = MedreEnvConfig.from_environ()
    assert env.content_retention_overrides["enabled"] == "true"
    assert env.content_retention_overrides["mode"] == "relay-only"


def test_field_names_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__enabled", "true")
    env = MedreEnvConfig.from_environ()
    assert env.content_retention_overrides["enabled"] == "true"


def test_unsupported_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Unsupported MEDRE_CONTENT_RETENTION__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_RETENTION__PROFILE": "x"})


def test_malformed_empty_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Malformed MEDRE_CONTENT_RETENTION__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_RETENTION__": "v"})


def test_malformed_extra_separator_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Malformed MEDRE_CONTENT_RETENTION__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_RETENTION__ENABLED__EXTRA": "v"})


def test_duplicate_normalized_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Duplicate normalized content_retention"
    ):
        MedreEnvConfig.from_environ(
            {
                "MEDRE_CONTENT_RETENTION__ENABLED": "true",
                "MEDRE_CONTENT_RETENTION__enabled": "false",
            }
        )


def test_overrides_apply_and_coerce(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__MODE", "relay-only")
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__INTERVAL_SECONDS", "45")
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__BATCH_LIMIT", "7")
    result = apply_env_overrides(base)
    assert result.content_retention.enabled is True
    assert result.content_retention.mode == "relay-only"
    assert result.content_retention.interval_seconds == 45
    assert result.content_retention.batch_limit == 7


def test_invalid_int_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__BATCH_LIMIT", "not-an-int")
    with pytest.raises(
        ConfigValidationError, match="MEDRE_CONTENT_RETENTION__BATCH_LIMIT"
    ):
        apply_env_overrides(_make_base_config())


def test_invalid_bool_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__ENABLED", "not-a-bool")
    with pytest.raises(ConfigValidationError, match="MEDRE_CONTENT_RETENTION__ENABLED"):
        apply_env_overrides(_make_base_config())


def test_cross_field_revalidation_rejects_ambiguous_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode alone does not enable: whole-section revalidation fails."""
    monkeypatch.setenv("MEDRE_CONTENT_RETENTION__MODE", "relay-only")
    with pytest.raises(ConfigValidationError, match="relay-only"):
        apply_env_overrides(_make_base_config())


def test_apply_empty_overrides_returns_same_config() -> None:
    base = _make_base_config()
    assert apply_content_retention_overrides(base, {}) is base


def test_apply_unknown_field_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="Unknown content_retention"):
        apply_content_retention_overrides(_make_base_config(), {"bogus": "1"})


def test_apply_directly_coerces_and_revalidates() -> None:
    result = apply_content_retention_overrides(
        _make_base_config(),
        {"enabled": "true", "mode": "relay-only", "batch_limit": "3"},
    )
    assert result.content_retention.enabled is True
    assert result.content_retention.mode == "relay-only"
    assert result.content_retention.batch_limit == 3


# ---------------------------------------------------------------------------
# Durable activation epochs — storage behavior
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, 0, minute, tzinfo=timezone.utc)


async def _seed_event_at(store, event_id: str, created_at: str, body: str) -> None:
    """Append one event and pin its local admission time deterministically.

    Pinning ``created_at`` avoids racing the wall clock's microsecond
    resolution when a test depends on admission ordering.
    """
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": body}, metadata={}
    )
    await store.append(event)
    await store._write(
        "UPDATE canonical_events SET created_at=? WHERE event_id=?",
        (created_at, event_id),
    )


async def test_open_epoch_records_in_transaction_admission_watermark(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "epoch.db"))
    await store.initialize()
    try:
        await _seed_event_at(store, "e1", "2026-10-31T10:00:00+00:00", "old")
        epoch = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 1)
        )
        assert epoch.epoch == 1
        assert epoch.mode == "relay-only"
        assert epoch.activated_at == "2026-11-01T00:00:00.000000+00:00"
        assert epoch.admission_watermark == "2026-10-31T10:00:00+00:00"
        assert epoch.last_observed_active_at is None
        assert epoch.activated_by == "operator"
        row = await store._read_one(
            "SELECT mode, activated_at, admission_watermark, "
            "last_observed_active_at, activated_by "
            "FROM content_retention_epochs WHERE epoch=1"
        )
        assert row["mode"] == "relay-only"
        assert row["admission_watermark"] == "2026-10-31T10:00:00+00:00"
    finally:
        await store.close()


async def test_open_epoch_on_empty_store_records_null_watermark(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "empty.db"))
    await store.initialize()
    try:
        epoch = await store.open_retention_epoch(
            activated_by="runtime-first-start", now=_utc(2026, 11, 1)
        )
        assert epoch.admission_watermark is None
        assert epoch.activated_by == "runtime-first-start"
    finally:
        await store.close()


async def test_watermark_is_monotonic_across_successive_epochs(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "mono.db"))
    await store.initialize()
    try:
        await _seed_event_at(store, "early", "2026-10-30T10:00:00+00:00", "old")
        first = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 1)
        )
        assert first.admission_watermark == "2026-10-30T10:00:00+00:00"
        await _seed_event_at(store, "later", "2026-11-02T10:00:00+00:00", "new")
        second = await store.open_retention_epoch(
            activated_by="runtime-first-start", now=_utc(2026, 11, 3)
        )
        assert second.epoch > first.epoch
        assert second.admission_watermark == "2026-11-02T10:00:00+00:00"
        assert second.admission_watermark > first.admission_watermark
        # A candidate admitted after activation is inside the boundary.
        await _seed_event_at(store, "post", "2026-11-04T10:00:00+00:00", "newer")
        third = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 5)
        )
        assert third.admission_watermark == "2026-11-04T10:00:00+00:00"
    finally:
        await store.close()


async def test_empty_store_keeps_previous_epoch_watermark(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "keep.db"))
    await store.initialize()
    try:
        first = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 1)
        )
        assert first.admission_watermark is None
        await _seed_event_at(store, "e1", "2026-11-01T12:00:00+00:00", "body")
        second = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 2)
        )
        assert second.admission_watermark == "2026-11-01T12:00:00+00:00"
    finally:
        await store.close()


async def test_latest_epoch_reads_the_durable_record(tmp_path) -> None:
    path = str(tmp_path / "durable.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        assert await store.latest_retention_epoch() is None
        opened = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 1)
        )
        latest = await store.latest_retention_epoch()
        assert latest == opened
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        latest = await reopened.latest_retention_epoch()
        assert latest is not None
        assert latest.epoch == opened.epoch
        assert latest.admission_watermark == opened.admission_watermark
        assert latest.activated_by == "operator"
    finally:
        await reopened.close()


async def test_heartbeat_never_moves_backward(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "beat.db"))
    await store.initialize()
    try:
        epoch = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 1)
        )
        await store.observe_retention_heartbeat(
            epoch=epoch.epoch, now=_utc(2026, 11, 2, minute=30)
        )
        # A clock rollback observes an earlier time: the durable
        # heartbeat stays at the latest observation.
        await store.observe_retention_heartbeat(
            epoch=epoch.epoch, now=_utc(2026, 11, 1)
        )
        latest = await store.latest_retention_epoch()
        assert latest is not None
        assert latest.last_observed_active_at == "2026-11-02T00:30:00.000000+00:00"
        # A later heartbeat advances it again.
        await store.observe_retention_heartbeat(
            epoch=epoch.epoch, now=_utc(2026, 11, 3)
        )
        latest = await store.latest_retention_epoch()
        assert latest is not None
        assert latest.last_observed_active_at == "2026-11-03T00:00:00.000000+00:00"
    finally:
        await store.close()


async def test_heartbeat_on_missing_epoch_fails_closed(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "missing.db"))
    await store.initialize()
    try:
        with pytest.raises(
            ContentCollectionEpochChanged,
            match="Retention activation changed before heartbeat",
        ):
            await store.observe_retention_heartbeat(epoch=99, now=_utc(2026, 11, 1))
    finally:
        await store.close()


async def test_open_epoch_refuses_unknown_activator_and_naive_time(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "refuse.db"))
    await store.initialize()
    try:
        with pytest.raises(ValueError, match="activator"):
            await store.open_retention_epoch(
                activated_by="scheduler", now=_utc(2026, 11, 1)
            )
        with pytest.raises(ValueError, match="timezone aware"):
            await store.open_retention_epoch(
                activated_by="operator",
                now=datetime(2026, 11, 1),
            )
        assert await store.latest_retention_epoch() is None
    finally:
        await store.close()


async def test_heartbeat_refuses_non_integer_epoch(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "int.db"))
    await store.initialize()
    try:
        with pytest.raises(ValueError, match="positive integer"):
            await store.observe_retention_heartbeat(
                epoch=True, now=_utc(2026, 11, 1)  # type: ignore[arg-type]
            )
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Bounded aggregate preview — read-only, metadata-only
# ---------------------------------------------------------------------------


def _preview_schema() -> dict:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "docs/schemas/content-retention-preview.schema.json"
    )
    return json.loads(schema_path.read_text())


async def test_no_active_epoch_is_an_honest_distinct_result(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "no-epoch.db"))
    await store.initialize()
    try:
        await _seed_eligible_at(store, "e1", "2026-11-01T10:00:00+00:00")
        report = await store.preview_content_retention()
        assert report["status"] == "no_active_epoch"
        assert report["events_examined"] == 0
        assert report["eligible"] == 0
        assert report["retention_authorized"] is False
        assert report["deletion_authorized"] is False
        assert report["physical_erasure_claimed"] is False
        assert report["has_more"] is False
        assert report["next_after"] is None
        Draft202012Validator.check_schema(_preview_schema())
        Draft202012Validator(_preview_schema()).validate(report)
    finally:
        await store.close()


async def test_preview_counts_are_content_free_and_schema_conformant(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "content-free.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 1))
        await _seed_eligible_at(store, "e1", "2026-11-02T10:00:00+00:00")
        report = await store.preview_content_retention()
        assert report["eligible"] == 1
        assert report["events_examined"] == 1
        assert report["mode"] == "relay-only"
        assert report["scope"] == "core_sqlite_epoch"
        assert report["unmeasured"] == list(COLLECTION_UNMEASURED_SCOPES)
        assert "status" not in report
        # No body text and no event-ID enumeration in the report: the
        # single next_after keyset cursor is the only identifier.
        dumped = json.dumps(report)
        assert "DO-NOT-DELETE" not in dumped
        assert report["next_after"] == ["2026-11-02T10:00:00+00:00", "e1"]
        assert dumped.count('"e1"') == 1
        Draft202012Validator(_preview_schema()).validate(report)
    finally:
        await store.close()


async def test_watermark_scoping_excludes_pre_activation_history(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "scope.db"))
    await store.initialize()
    try:
        # A settled pre-activation event with eligible content is never
        # a candidate: the boundary is strictly after the watermark.
        await _seed_eligible_at(store, "historic", "2026-10-31T10:00:00+00:00")
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 1))
        await _seed_eligible_at(store, "fresh", "2026-11-02T10:00:00+00:00")
        report = await store.preview_content_retention()
        assert report["admission_watermark"] == "2026-10-31T10:00:00+00:00"
        assert report["events_examined"] == 1
        assert report["eligible"] == 1
    finally:
        await store.close()


async def test_preview_maps_blocked_empty_already_collected_and_inconsistent(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "outcomes.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 1))
        # eligible: settled ordinary text.
        await _seed_eligible_at(store, "ok", "2026-11-02T10:00:00+00:00")
        # blocked: a live owner (unsettled append dependency).
        event = msgspec.structs.replace(
            make_storage_event(event_id="live"),
            payload={"body": "DO-NOT-DELETE-live"},
            metadata={},
        )
        await store.append(event)
        await store._write(
            "UPDATE canonical_events SET created_at=? WHERE event_id='live'",
            ("2026-11-02T11:00:00+00:00",),
        )
        # empty: settled event whose admission recorded no body.
        event = msgspec.structs.replace(
            make_storage_event(event_id="void"), payload={}, metadata={}
        )
        await store.append(event)
        await store._write(
            "UPDATE canonical_events SET created_at=? WHERE event_id='void'",
            ("2026-11-02T12:00:00+00:00",),
        )
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id='void' AND owner_kind='append'",
            ("2026-10-31T01:00:00+00:00",),
        )
        # already_collected: eligible, then collected once.
        await _seed_eligible_at(store, "done", "2026-11-02T13:00:00+00:00")
        assert (await store.collect_event_content("done", dry_run=False))[
            "status"
        ] == "collected"
        # inconsistent: admission identity says no body, yet a body row
        # exists — the collector's fail-closed StorageError path.
        event = msgspec.structs.replace(
            make_storage_event(event_id="corrupt"), payload={}, metadata={}
        )
        await store.append(event)
        await store._write(
            "UPDATE canonical_events SET created_at=? WHERE event_id='corrupt'",
            ("2026-11-02T14:00:00+00:00",),
        )
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id='corrupt' AND owner_kind='append'",
            ("2026-10-31T01:00:00+00:00",),
        )
        await store._write(
            "INSERT INTO event_body_content(event_id,field,body,created_at) "
            "VALUES ('corrupt','body','DO-NOT-DELETE-corrupt','2026-11-02')"
        )
        report = await store.preview_content_retention()
        assert report["eligible"] == 1
        assert report["blocked"] == 1
        assert report["empty"] == 1
        assert report["already_collected"] == 1
        assert report["inconsistent"] == 1
        assert report["events_examined"] == 5
        dumped = json.dumps(report)
        assert "DO-NOT-DELETE" not in dumped
        Draft202012Validator(_preview_schema()).validate(report)
    finally:
        await store.close()


async def test_preview_bounds_pages_and_reports_continuation(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "pages.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 1))
        for index in range(3):
            await _seed_eligible_at(
                store,
                f"p{index}",
                f"2026-11-02T10:0{index}:00+00:00",
            )
        report = await store.preview_content_retention(limit=2)
        assert report["events_examined"] == 2
        assert report["eligible"] == 2
        assert report["has_more"] is True
        assert report["next_after"] == ["2026-11-02T10:01:00+00:00", "p1"]
        next_page = await store.preview_content_retention(
            limit=2, after=tuple(report["next_after"])
        )
        assert next_page["events_examined"] == 1
        assert next_page["eligible"] == 1
        assert next_page["has_more"] is False
        assert next_page["next_after"] == ["2026-11-02T10:02:00+00:00", "p2"]
        full = await store.preview_content_retention()
        assert full["events_examined"] == 3
        assert full["has_more"] is False
        assert full["next_after"] == ["2026-11-02T10:02:00+00:00", "p2"]
        # No epoch-scoped candidate at all: null continuation.
        empty_store = SQLiteStorage(str(tmp_path / "pages-empty.db"))
        await empty_store.initialize()
        try:
            await empty_store.open_retention_epoch(
                activated_by="operator", now=_utc(2026, 11, 1)
            )
            report = await empty_store.preview_content_retention()
            assert report["events_examined"] == 0
            assert report["has_more"] is False
            assert report["next_after"] is None
        finally:
            await empty_store.close()
    finally:
        await store.close()


async def test_preview_limit_is_bounded_and_validated(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "limit.db"))
    await store.initialize()
    try:
        with pytest.raises(ValueError, match="limit"):
            await store.preview_content_retention(limit=0)
        with pytest.raises(ValueError, match="limit"):
            await store.preview_content_retention(limit=101)
        with pytest.raises(ValueError, match="limit"):
            await store.preview_content_retention(limit=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="cursor"):
            await store.preview_content_retention(after=("only-one", ""))
        with pytest.raises(ValueError, match="cursor"):
            await store.preview_content_retention(after=("", "event"))
    finally:
        await store.close()


async def test_preview_is_read_only(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "readonly.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 1))
        await _seed_eligible_at(store, "kept", "2026-11-02T10:00:00+00:00")
        before = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id='kept'"
        )
        report = await store.preview_content_retention()
        assert report["eligible"] == 1
        after = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id='kept'"
        )
        assert before == after
        receipts = await store._read_one(
            "SELECT COUNT(*) AS n FROM event_content_collection_log"
        )
        assert receipts["n"] == 0
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Collected-receipt aggregate — the one read-only count for operator status
# ---------------------------------------------------------------------------


async def test_count_collected_events_aggregates_durable_receipts_only(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "count.db"))
    await store.initialize()
    try:
        assert await store.count_collected_events() == 0
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 1))
        await _seed_eligible_at(store, "count-1", "2026-11-02T10:00:00+00:00")
        await _seed_eligible_at(store, "count-2", "2026-11-02T11:00:00+00:00")
        assert (await store.collect_event_content("count-1", dry_run=False))[
            "status"
        ] == "collected"
        assert await store.count_collected_events() == 1
        # Re-collection adds no receipt: the count tracks the durable rows.
        assert (await store.collect_event_content("count-1", dry_run=False))[
            "status"
        ] == "already_collected"
        assert await store.count_collected_events() == 1
        assert (await store.collect_event_content("count-2", dry_run=False))[
            "status"
        ] == "collected"
        assert await store.count_collected_events() == 2
    finally:
        await store.close()


# Sample config parity — the generated sample declares the disabled default
# ---------------------------------------------------------------------------


def test_sample_config_declares_disabled_retention() -> None:
    from medre.config._yaml import parse_yaml_config
    from medre.config.sample import generate_sample_config

    parsed = parse_yaml_config(generate_sample_config())
    section = parsed["content_retention"]
    assert section["enabled"] is False
    assert section["mode"] == "retain"
    config = parse_runtime_config(parsed, resolve())
    assert config.content_retention == ContentRetentionConfig().validate()


def test_runtime_schema_matches_shared_retention_bounds():
    from pathlib import Path

    from medre.core.storage.content_retention import (
        RETENTION_BATCH_MAX,
        RETENTION_BATCH_MIN,
        RETENTION_INTERVAL_MAX,
        RETENTION_INTERVAL_MIN,
        RETENTION_POLICY_MODES,
        RETENTION_POLICY_VERSION,
    )

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/runtime-config.schema.json"
        ).read_text()
    )["properties"]["content_retention"]["properties"]
    assert schema["version"]["const"] == RETENTION_POLICY_VERSION
    assert set(schema["mode"]["enum"]) == RETENTION_POLICY_MODES
    assert (
        schema["interval_seconds"]["minimum"],
        schema["interval_seconds"]["maximum"],
    ) == (RETENTION_INTERVAL_MIN, RETENTION_INTERVAL_MAX)
    assert (schema["batch_limit"]["minimum"], schema["batch_limit"]["maximum"]) == (
        RETENTION_BATCH_MIN,
        RETENTION_BATCH_MAX,
    )
