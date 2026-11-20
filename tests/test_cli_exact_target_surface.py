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

import json
import os
from pathlib import Path

import pytest

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
from tests.helpers.cli import _replay_cli
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
    with pytest.raises(ValueError, match="blank string is not a channel"):
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


# Every persisted delivery plan id is the canonical five-component shape
# ``plan:{event}:{route}:{index}:{hash}``, so the compact ``--exact-target``
# value must express a plan component that itself contains colons. The
# identities below are fabricated placeholders in the real shape.
_EVENT = "evt-4c1a9b02"
_OTHER_EVENT = "evt-7d2b3e01"
_CANONICAL_PLAN = f"plan:{_EVENT}:mx-bridge:target:0f1e2d3c4b5a6978"


def test_colon_bearing_plan_id_round_trips_to_one_identity():
    identity = _selection(
        f"{_EVENT}:{_CANONICAL_PLAN}:matrix:!a4b3c2d1:mesh.example.org"
    )
    assert identity == DeliveryIdentity(
        _EVENT, _CANONICAL_PLAN, "matrix", "!a4b3c2d1:mesh.example.org"
    )


def test_colon_bearing_plan_id_names_the_no_channel_identity():
    identity = _selection(f"{_EVENT}:{_CANONICAL_PLAN}:matrix")
    assert identity == DeliveryIdentity(_EVENT, _CANONICAL_PLAN, "matrix", None)


def test_explicit_no_channel_flag_accepts_a_colon_bearing_plan_id():
    identity = _selection(f"{_EVENT}:{_CANONICAL_PLAN}:matrix", no_channel=True)
    assert identity == DeliveryIdentity(_EVENT, _CANONICAL_PLAN, "matrix", None)


def test_colon_bearing_plan_for_another_event_is_rejected_loudly():
    other_plan = f"plan:{_OTHER_EVENT}:mx-bridge:target:0f1e2d3c4b5a6978"
    with pytest.raises(ValueError, match=f"embeds event '{_OTHER_EVENT}'"):
        _parse_exact_target_specs([f"{_EVENT}:{other_plan}:matrix"], None)


def test_colon_bearing_plan_with_blank_component_is_rejected():
    with pytest.raises(ValueError, match="nonblank"):
        _parse_exact_target_specs(
            [f"{_EVENT}:plan:{_EVENT}::target:0f1e2d3c4b5a6978:matrix"], None
        )


def test_colon_free_plan_keeps_the_historical_split():
    # Six-plus components with a colon-free plan component keep the
    # historical reading: the channel is everything after the third colon.
    identity = _selection("evt:plan:adapter:a:b:c:d")
    assert identity == DeliveryIdentity("evt", "plan", "adapter", "a:b:c:d")


def test_short_plan_placeholder_keeps_the_historical_split():
    # The documented compact examples name the plan component literally
    # ``plan``; with fewer than six remaining components they keep parsing
    # as plan/adapter/channel, never as a colon-bearing plan id.
    identity = _selection("evt:plan:adapter:!a4b3c2d1:mesh.example.org")
    assert identity == DeliveryIdentity(
        "evt", "plan", "adapter", "!a4b3c2d1:mesh.example.org"
    )


# ---------------------------------------------------------------------------
# Replay: explicit JSON identity form
#
# The compact colon spelling cannot distinguish every identity from the
# overloaded string alone, so --exact-target-json names the four identity
# fields positionally: ["EVENT", "TARGET", "ADAPTER", "CHANNEL-or-null"].
# ---------------------------------------------------------------------------


def _json_selection(value: str) -> DeliveryIdentity:
    selections = _parse_exact_target_specs(None, None, [value])
    assert len(selections) == 1
    return selections[0].identity


def test_json_identity_round_trips_colon_bearing_fields():
    identity = _json_selection(
        '["event:1","plan:event:1:mx-bridge:target:0f1e2d3c4b5a6978",'
        '"matrix","!room:example.org"]'
    )
    assert identity == DeliveryIdentity(
        "event:1",
        "plan:event:1:mx-bridge:target:0f1e2d3c4b5a6978",
        "matrix",
        "!room:example.org",
    )


def test_json_identity_accepts_a_colon_bearing_event_id():
    # The compact spelling has no reading at all for a colon-bearing event
    # id; the JSON form carries it byte-exact.
    identity = _json_selection('["event:1","plan","matrix",null]')
    assert identity == DeliveryIdentity("event:1", "plan", "matrix", None)


def test_json_identity_null_channel_names_the_no_channel_identity():
    identity = _json_selection('["evt","plan","adapter",null]')
    assert identity == DeliveryIdentity("evt", "plan", "adapter", None)


def test_json_selections_accumulate_across_repeats_and_compact_flags():
    selections = _parse_exact_target_specs(
        ["evt:plan:adapter"],
        None,
        [
            '["evt","plan:evt:route:target:0f1e2d3c4b5a6978","matrix",'
            '"!room:example.org"]',
            '["evt-2","plan","adapter",null]',
        ],
    )
    assert [s.identity for s in selections] == [
        DeliveryIdentity("evt", "plan", "adapter", None),
        DeliveryIdentity(
            "evt",
            "plan:evt:route:target:0f1e2d3c4b5a6978",
            "matrix",
            "!room:example.org",
        ),
        DeliveryIdentity("evt-2", "plan", "adapter", None),
    ]


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        '{"event":"evt","plan":"plan"}',
        '"evt"',
        "42",
        "null",
        "[]",
        '["evt","plan"]',
        '["evt","plan","adapter"]',
        '["evt","plan","adapter","ch","extra"]',
        '[1,"plan","adapter",null]',
        '["evt",null,"adapter",null]',
        '["evt","plan","adapter",7]',
        '["evt","plan","adapter",false]',
        '["","plan","adapter",null]',
        '["  ","plan","adapter",null]',
        '["evt","  ","adapter",null]',
        '["evt","plan","  ",null]',
        '["evt","plan","adapter",""]',
        '["evt","plan","adapter","  "]',
    ],
)
def test_malformed_json_identity_is_rejected(value: str):
    with pytest.raises(ValueError, match="Invalid --exact-target-json"):
        _parse_exact_target_specs(None, None, [value])


def test_json_identity_duplicate_across_flags_is_rejected():
    with pytest.raises(ValueError, match="Duplicate exact-target"):
        _parse_exact_target_specs(
            ["evt:plan:adapter"], None, ['["evt","plan","adapter",null]']
        )


def test_legacy_event_named_plan_with_colon_channel_is_a_near_collision():
    """The near-collision that motivates the explicit JSON grammar.

    An event id literally ``plan`` with a colon-bearing channel tail is
    byte-indistinguishable from the canonical plan-id spelling of a
    different obligation, so the compact string cannot reliably carry the
    legacy reading: hash-tailed bytes are refused loudly, and the
    plan-shaped bytes are consumed as a canonical plan id. The JSON form
    names the four fields explicitly and selects exactly the named
    obligation — distinct from what the compact bytes produce.
    """
    channel = "!room:example.org:0f1e2d3c4b5a6978:tail"
    # The legacy compact bytes sit inside the canonical plan-id shape, so
    # the conservative detection refuses them instead of guessing.
    with pytest.raises(ValueError, match="embeds event"):
        _parse_exact_target_specs([f"plan:plan:matrix:{channel}"], None)
    # The JSON spelling carries the intended legacy obligation byte-exact.
    legacy = _json_selection(
        '["plan","plan","matrix","!room:example.org:0f1e2d3c4b5a6978:tail"]'
    )
    assert legacy == DeliveryIdentity(
        "plan", "plan", "matrix", "!room:example.org:0f1e2d3c4b5a6978:tail"
    )
    # Plan-shaped compact bytes are consumed as a canonical plan id — a
    # different obligation than the JSON spelling of the same leading
    # event id, which is exactly the ambiguity the JSON form removes.
    compact = _selection("plan:plan:plan:route:target:0f1e2d3c4b5a6978:matrix")
    assert compact == DeliveryIdentity(
        "plan", "plan:plan:route:target:0f1e2d3c4b5a6978", "matrix", None
    )
    assert compact != legacy


# ---------------------------------------------------------------------------
# Replay: CLI exit codes and wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "specs",
    [
        ("--exact-target", "evt:plan"),
        ("--exact-target", "evt:plan:adapter:"),
        ("--exact-target", "   :plan:adapter"),
        ("--exact-target", "evt:  :adapter"),
        ("--exact-target", "evt:plan:  "),
        ("--exact-target", "evt:plan:adapter:  "),
        ("--exact-no-channel", "evt:plan:  "),
        ("--exact-no-channel", "evt:plan:adapter:!room"),
        ("--exact-target", "evt:plan:adapter", "--exact-target", "evt:plan:adapter"),
        (
            "--exact-target",
            "evt-4c1a9b02:plan:evt-7d2b3e01:mx-bridge:target:0f1e2d3c4b5a6978:matrix",
        ),
        ("--exact-target-json", "not json"),
        ("--exact-target-json", '{"event":"evt"}'),
        ("--exact-target-json", '["evt","plan"]'),
        ("--exact-target-json", '["evt","plan","adapter","ch","extra"]'),
        ("--exact-target-json", '[1,"plan","adapter",null]'),
        ("--exact-target-json", '["evt","plan","adapter",""]'),
        (
            "--exact-target",
            "evt:plan:adapter",
            "--exact-target-json",
            '["evt","plan","adapter",null]',
        ),
    ],
)
def test_malformed_exact_target_specifications_exit_with_config_error(
    tmp_path: Path, specs
):
    config_path = tmp_path / "unused.yaml"
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
    assert not config_path.exists()


def test_colon_bearing_plan_specification_passes_the_config_gate(
    tmp_path: Path,
) -> None:
    """A canonical colon-bearing plan id parses before config loading.

    With a missing config the replay command reaches the config-error
    boundary, proving the exact-target value itself was accepted.
    """
    config_path = tmp_path / "missing-config.yaml"
    code, _stdout, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "dry_run",
        "--event",
        "whatever",
        "--exact-target",
        f"{_EVENT}:{_CANONICAL_PLAN}:matrix:!a4b3c2d1:mesh.example.org",
    )
    assert code == 2
    assert "Invalid --exact-target" not in stderr
    assert "Config error" in stderr


def test_json_identity_specification_passes_the_config_gate(tmp_path: Path) -> None:
    """The explicit JSON identity form parses before config loading.

    A colon-bearing event id and channel that have no unambiguous compact
    spelling are accepted through the JSON form: with a missing config the
    replay command reaches the config-error boundary, proving the value
    itself was accepted, and repeated flags accumulate without error.
    """
    config_path = tmp_path / "missing-config.yaml"
    code, _stdout, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "dry_run",
        "--event",
        "whatever",
        "--exact-target-json",
        '["event:1","plan:event:1:mx-bridge:target:0f1e2d3c4b5a6978",'
        '"matrix","!room:example.org"]',
        "--exact-target-json",
        '["event:1","plan","matrix",null]',
    )
    assert code == 2
    assert "Invalid --exact-target-json" not in stderr
    assert "Config error" in stderr


def test_invalid_request_is_rejected_before_loading_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.cli import replay_commands

    def invalid_request(**_kwargs):
        raise ValueError("unacceptable execution key")

    monkeypatch.setattr(replay_commands, "ReplayRequest", invalid_request)
    config_path = tmp_path / "missing-config.yaml"
    code, _stdout, stderr = _replay_cli(
        "replay", "--config", str(config_path), "--mode", "best_effort"
    )
    assert code == 2
    assert "invalid replay request: unacceptable execution key" in stderr
    assert not config_path.exists()
