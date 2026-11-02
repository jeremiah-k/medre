"""Operator surface for exact-target decisions and replay selection.

Resolve-side tests prove the ``medre resolve --action`` preview/apply flow
over the audited decision authority: private-local ownership, metadata-only
previews, idempotent applies, and refusal rendering. Replay-side tests prove
``--exact-target``/``--exact-no-channel`` validation and plumbing into
``ReplayRequest.exact_target_selections``. Every resolve test calls the real
``_resolve_unstarted`` handler; every replay exit-code test calls
``main(["replay", ...])``.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from medre.cli import main as cli_main
from medre.cli.main import _build_parser
from medre.cli.replay_commands import _parse_exact_target_specs
from medre.cli.resolve_commands import _resolve_unstarted
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.storage.sqlite._operator_decision import (
    ACTIVE_NATIVE_UNDECIDABLE,
    COMPETING_GENERATION,
    EXACT_TARGET_GONE,
    SETTLED_TARGET_UNDECIDABLE,
    TERMINAL_REOPEN_REFUSED,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.config import loader as _config_loader
from tests.helpers.storage_outbox import make_outbox_item

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX owner boundary")


# ---------------------------------------------------------------------------
# Resolve-side helpers
# ---------------------------------------------------------------------------


def _args(path: str, *options: str):
    return _build_parser(include_adapter_cli=False).parse_args(
        [
            "resolve",
            "--storage-path",
            path,
            "--event",
            "test-001",
            "--plan-id",
            "plan",
            "--target-adapter",
            "matrix",
            "--target-channel",
            "!left",
            *options,
        ]
    )


def _no_channel_args(path: str, *options: str):
    return _build_parser(include_adapter_cli=False).parse_args(
        [
            "resolve",
            "--storage-path",
            path,
            "--event",
            "test-001",
            "--plan-id",
            "plan",
            "--target-adapter",
            "matrix",
            "--no-channel",
            *options,
        ]
    )


async def _seed_two_legs(path: str, sample_event) -> None:
    store = SQLiteStorage(path)
    await store.initialize()
    await store.append(sample_event)
    for channel in ("!left", "!right"):
        await store.create_outbox_item(
            make_outbox_item(
                delivery_plan_id="plan",
                target_adapter="matrix",
                target_channel=channel,
                event_id=sample_event.event_id,
            )
        )
    await store.close()


async def _claim_all(path: str) -> None:
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.claim_due_outbox_items(
            now="2030-01-01T00:00:00", worker_id="native-owner", limit=10
        )
    finally:
        await store.close()


async def _mark_channel(
    path: str,
    event_id: str,
    channel: str,
    mark: str,
) -> None:
    """Move one leg to a terminal status through the production transitions."""
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        items = await store.list_outbox_items_for_event(event_id)
        target = next(item for item in items if item.target_channel == channel)
        if mark == "dead_lettered":
            await store.claim_due_outbox_items(
                now="2030-01-01T00:00:00", worker_id="native-owner", limit=10
            )
            await store.mark_outbox_dead_lettered(
                target.outbox_id, failure_kind="retry_exhausted"
            )
        elif mark == "cancelled":
            await store.mark_outbox_cancelled(target.outbox_id)
        else:  # pragma: no cover - test-only guard
            raise AssertionError(f"unsupported mark: {mark}")
    finally:
        await store.close()


def _apply_options(observed: dict, decision_id: str, action: str) -> list[str]:
    return [
        "--action",
        action,
        "--apply",
        "--yes",
        "--decision-id",
        decision_id,
        "--actor-label",
        "operator-provenance",
        "--outbox-id",
        str(observed["outbox_id"]),
        "--expected-generation",
        str(observed["expected_generation"]),
        "--expected-status",
        str(observed["expected_status"]),
        "--expected-updated-at",
        str(observed["expected_updated_at"]),
    ]


# ---------------------------------------------------------------------------
# Resolve: decision preview
# ---------------------------------------------------------------------------


@posix_only
async def test_decision_preview_reports_decidable_observation(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_authorized"] is False
    assert payload["decision_capable"] is True
    preview = payload["preview"]
    assert preview["category"] == "pending"
    assert preview["target_channel"] == "!left"
    assert preview["attempt_number"] == 1
    assert preview["active_attempt"] is None
    assert preview["cancel_eligible"] is True
    assert preview["execution_authorized"] is False
    observed = payload["observed"]
    assert observed["target_channel"] == "!left"
    assert observed["expected_generation"] == 1
    assert observed["expected_status"] == "pending"
    assert observed["expected_updated_at"]
    assert observed["outbox_id"]
    assert "refusal_implications" not in payload


@posix_only
async def test_decision_preview_selects_the_no_channel_identity(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    await store.append(sample_event)
    await store.create_outbox_item(
        make_outbox_item(
            delivery_plan_id="plan",
            target_adapter="matrix",
            target_channel=None,
            event_id=sample_event.event_id,
        )
    )
    await store.close()
    Path(path).chmod(0o600)
    await _resolve_unstarted(_no_channel_args(path, "--action", "resolve_uncertain"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision_capable"] is True
    assert payload["preview"]["target_channel"] is None
    assert payload["observed"]["expected_status"] == "pending"


@posix_only
async def test_decision_preview_names_active_native_refusal(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    await _claim_all(path)
    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision_capable"] is False
    assert "observed" not in payload
    assert payload["refusal_implications"] == [ACTIVE_NATIVE_UNDECIDABLE]


@posix_only
async def test_decision_preview_names_terminal_reopen_refusal(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    await _mark_channel(path, sample_event.event_id, "!left", "dead_lettered")
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision_capable"] is False
    assert payload["preview"]["category"] == "preview_replay"
    assert payload["refusal_implications"] == [TERMINAL_REOPEN_REFUSED]


@posix_only
async def test_decision_preview_names_settled_refusal(tmp_path, sample_event, capsys):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    await _mark_channel(path, sample_event.event_id, "!left", "cancelled")
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resolve_uncertain"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision_capable"] is False
    assert payload["preview"]["category"] == "cancelled"
    assert payload["refusal_implications"] == [SETTLED_TARGET_UNDECIDABLE]


@posix_only
async def test_decision_preview_names_absent_and_contested_identity_refusals(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    missing = _build_parser(include_adapter_cli=False).parse_args(
        [
            "resolve",
            "--storage-path",
            path,
            "--event",
            "test-001",
            "--plan-id",
            "plan",
            "--target-adapter",
            "matrix",
            "--target-channel",
            "!missing",
            "--action",
            "resume_scheduled",
        ]
    )
    await _resolve_unstarted(missing)
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision_capable"] is False
    assert payload["preview"]["category"] == "ambiguous"
    assert payload["refusal_implications"] == [
        EXACT_TARGET_GONE,
        COMPETING_GENERATION,
    ]


# ---------------------------------------------------------------------------
# Resolve: decision apply
# ---------------------------------------------------------------------------


@posix_only
async def test_decision_apply_commits_once_and_never_touches_the_row(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    args = _args(path, *_apply_options(observed, "choice-1", "resume_scheduled"))
    await _resolve_unstarted(args)
    assert json.loads(capsys.readouterr().out) == {
        "decision_id": "choice-1",
        "committed": True,
    }
    await _resolve_unstarted(args)
    assert json.loads(capsys.readouterr().out)["committed"] is False

    store = await SQLiteStorage.open_readonly(path)
    try:
        outboxes = await store.list_outbox_items_for_event(sample_event.event_id)
        assert {x.target_channel: x.status for x in outboxes} == {
            "!left": "pending",
            "!right": "pending",
        }
        decisions = await store.list_operator_decisions(sample_event.event_id)
        assert len(decisions) == 1
        assert decisions[0]["action"] == "resume_scheduled"
        assert decisions[0]["actor_id"] == "operator-provenance"
        assert decisions[0]["observed_status"] == "pending"
    finally:
        await store.close()


@posix_only
async def test_resolve_uncertain_apply_records_the_audit_row(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resolve_uncertain"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    await _resolve_unstarted(
        _args(path, *_apply_options(observed, "choice-2", "resolve_uncertain"))
    )
    assert json.loads(capsys.readouterr().out)["committed"] is True
    store = await SQLiteStorage.open_readonly(path)
    try:
        decisions = await store.list_operator_decisions(sample_event.event_id)
        assert [row["action"] for row in decisions] == ["resolve_uncertain"]
    finally:
        await store.close()


@posix_only
async def test_decision_id_rebind_refuses(tmp_path, sample_event, capsys):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    await _resolve_unstarted(
        _args(path, *_apply_options(observed, "choice-1", "resume_scheduled"))
    )
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(
            _args(path, *_apply_options(observed, "choice-1", "resolve_uncertain"))
        )
    assert exc.value.code == 1
    assert "Decision ID already binds another request" in capsys.readouterr().err
    store = await SQLiteStorage.open_readonly(path)
    try:
        decisions = await store.list_operator_decisions(sample_event.event_id)
        assert [row["action"] for row in decisions] == ["resume_scheduled"]
    finally:
        await store.close()


@posix_only
async def test_stale_observation_refuses_the_apply(tmp_path, sample_event, capsys):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    await _claim_all(path)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(
            _args(path, *_apply_options(observed, "late-1", "resume_scheduled"))
        )
    assert exc.value.code == 1
    assert ACTIVE_NATIVE_UNDECIDABLE in capsys.readouterr().err
    store = await SQLiteStorage.open_readonly(path)
    try:
        assert await store.list_operator_decisions(sample_event.event_id) == []
    finally:
        await store.close()


@posix_only
async def test_cli_cannot_express_a_decision_over_a_settled_row(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    await _mark_channel(path, sample_event.event_id, "!left", "cancelled")
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(
            _args(
                path,
                "--action",
                "resume_scheduled",
                "--apply",
                "--yes",
                "--decision-id",
                "settled-1",
                "--actor-label",
                "operator",
                "--outbox-id",
                "obox",
                "--expected-generation",
                "1",
                "--expected-status",
                "cancelled",
                "--expected-updated-at",
                "2026-01-01T00:00:00+00:00",
            )
        )
    assert exc.value.code == 2
    assert "invalid choice: 'cancelled'" in capsys.readouterr().err


@posix_only
async def test_decision_apply_requires_confirmation_and_observed_state(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(
            _args(
                path,
                "--action",
                "resume_scheduled",
                "--apply",
                "--decision-id",
                "d",
                "--actor-label",
                "a",
                "--outbox-id",
                "obox",
                "--expected-generation",
                "1",
                "--expected-status",
                "pending",
                "--expected-updated-at",
                "2026-01-01T00:00:00+00:00",
            )
        )
    assert exc.value.code == 2
    assert "--yes" in capsys.readouterr().err

    await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    partial = _apply_options(observed, "d", "resume_scheduled")
    for flag in ("--outbox-id", "--expected-updated-at"):
        idx = partial.index(flag)
        stripped = partial[:idx] + partial[idx + 2 :]
        with pytest.raises(SystemExit) as exc:
            await _resolve_unstarted(_args(path, *stripped))
        assert exc.value.code == 2
        assert flag in capsys.readouterr().err


@posix_only
async def test_decision_preview_requires_the_private_local_store(
    tmp_path, sample_event
):
    path = str(tmp_path / "state.sqlite")
    await _seed_two_legs(path, sample_event)
    Path(path).chmod(0o644)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(path, "--action", "resume_scheduled"))
    assert exc.value.code == 3


# ---------------------------------------------------------------------------
# Replay: exact-target specification parsing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def _selection(spec: str, *, no_channel: bool = False) -> DeliveryIdentity:
    selections = (
        _parse_exact_target_specs([spec], None)
        if not no_channel
        else _parse_exact_target_specs(None, [spec])
    )
    assert len(selections) == 1
    return selections[0].identity


def test_three_component_exact_target_selects_the_no_channel_identity():
    identity = _selection("evt:plan:adapter")
    assert identity == DeliveryIdentity("evt", "plan", "adapter", None)


def test_four_component_exact_target_names_one_channel():
    identity = _selection("evt:plan:adapter:!room:server")
    assert identity == DeliveryIdentity("evt", "plan", "adapter", "!room:server")


def test_explicit_no_channel_flag_selects_the_no_channel_identity():
    identity = _selection("evt:plan:adapter", no_channel=True)
    assert identity == DeliveryIdentity("evt", "plan", "adapter", None)


def test_empty_channel_component_is_rejected_never_widened():
    with pytest.raises(ValueError, match="empty string is not a channel"):
        _parse_exact_target_specs(["evt:plan:adapter:"], None)


def test_incomplete_identity_is_rejected():
    for spec in ("evt", "evt:plan"):
        with pytest.raises(ValueError, match="expected"):
            _parse_exact_target_specs([spec], None)
    with pytest.raises(ValueError, match="expected exactly"):
        _parse_exact_target_specs(None, ["evt:plan:adapter:!room"])
    with pytest.raises(ValueError, match="Invalid --exact-target"):
        _parse_exact_target_specs(["evt::adapter"], None)


def test_repeated_identity_across_flags_is_rejected():
    with pytest.raises(ValueError, match="Duplicate exact-target"):
        _parse_exact_target_specs(["evt:plan:adapter"], ["evt:plan:adapter"])
    with pytest.raises(ValueError, match="Duplicate exact-target"):
        _parse_exact_target_specs(["evt:plan:adapter:ch", "evt:plan:adapter:ch"], None)


# ---------------------------------------------------------------------------
# Replay: CLI exit codes and wiring
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _declared_replay_transports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare the transports the replay fixtures name.

    Transport-group validation reads the import-time snapshot of installed
    adapters, which is empty in adapter-free environments. The fixtures
    name these groups themselves, mirroring the bridge compatibility
    suites that declare the transports their configs use.
    """
    declared = frozenset({"matrix", "meshtastic", "fake_meshtastic"})
    monkeypatch.setattr(
        _config_loader,
        "_KNOWN_TRANSPORTS",
        declared | frozenset(_config_loader._KNOWN_TRANSPORTS),
    )


_REPLAY_YAML = """\
runtime:
  name: exact-target-replay-surface
  shutdown_timeout_seconds: 10

logging:
  level: WARNING
  format: text

storage:
  backend: sqlite
  path: "{storage_path}"

adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: fake
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: exact-target-surface

routes:
  mx_to_mesh:
    source_adapters:
      - fake_matrix
    dest_adapters:
      - fake_meshtastic
    directionality: source_to_dest
    enabled: true
"""


def _replay_cli(*options: str) -> tuple[int, str, str]:
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        try:
            cli_main(list(options))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _seed_replay_db(tmp_path: Path) -> tuple[str, Path]:
    db_path = tmp_path / "exact_target_replay.db"
    config_path = tmp_path / "exact_target_replay.yaml"
    config_path.write_text(_REPLAY_YAML.format(storage_path=str(db_path)))
    code, stdout, stderr = _replay_cli(
        "smoke",
        "--config",
        str(config_path),
        "--json",
    )
    assert code == 0, f"smoke seed failed: {stderr}; stdout: {stdout[:2000]}"
    report = json.loads(stdout)
    assert report["status"] == "passed", f"smoke seed report: {stdout[:2000]}"
    return report["event_id"], config_path


@pytest.mark.parametrize(
    "specs",
    [
        ("--exact-target", "evt:plan"),
        ("--exact-target", "evt:plan:adapter:"),
        ("--exact-no-channel", "evt:plan:adapter:!room"),
        ("--exact-target", "evt:plan:adapter", "--exact-target", "evt:plan:adapter"),
    ],
)
def test_malformed_exact_target_specifications_exit_with_config_error(
    tmp_path: Path, specs
):
    config_path = tmp_path / "unused.yaml"
    config_path.write_text(_REPLAY_YAML.format(storage_path=str(tmp_path / "u.db")))
    code, _, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "dry_run",
        "--event",
        "whatever",
        *specs,
    )
    assert code == 2
    assert "Invalid" in stderr or "Duplicate" in stderr


def test_dry_run_accepts_exact_target_selections(tmp_path: Path):
    event_id, config_path = _seed_replay_db(tmp_path)
    code, stdout, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "dry_run",
        "--event",
        event_id,
        "--json",
        "--exact-target",
        "evt:plan:adapter:!room:server",
        "--exact-no-channel",
        "evt2:plan2:adapter2",
    )
    assert code == 0, f"dry_run failed: {stderr}"
    summary = json.loads(stdout)
    assert summary["mode"] == "dry_run"
    assert summary["events_replayed"] >= 4


def test_best_effort_admits_nothing_outside_the_named_selection(tmp_path: Path):
    event_id, config_path = _seed_replay_db(tmp_path)
    db_path = tmp_path / "exact_target_replay.db"
    code, stdout, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "best_effort",
        "--event",
        event_id,
        "--json",
        "--exact-target",
        "absent:plan:adapter:!room:server",
    )
    assert code == 0, f"best_effort failed: {stderr}"
    summary = json.loads(stdout)
    assert summary["mode"] == "best_effort"
    # The named identity matches no persisted delivery plan, so delivery
    # admission admits no new generation for the seeded event.
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE event_id=?", (event_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1
