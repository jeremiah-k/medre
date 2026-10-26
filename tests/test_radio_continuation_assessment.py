"""Read-only continuation assessment cannot become a radio grant."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

from medre.cli.inspect_commands import _inspect_radio_attempt
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.recovery.radio_assessment import assess_radio_continuation
from medre.core.rendering.radio_progress import RadioAttemptProgress
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import (
    RadioLocalHandoff,
    plain_channel_payload_fingerprint,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)


def _retained() -> tuple[DeliveryAttemptProvenance, RadioAttemptProgress]:
    """Construct a valid original acceptance followed by an unsent suffix."""
    provenance = DeliveryAttemptProvenance(
        "event-a", "plan-a", "meshcore", "0", "outbox-a", 1, "live"
    )
    plan = plan_radio_text("café α " * 25, max_text_bytes=35)
    progress = RadioAttemptProgress.bind(
        outbox_id="outbox-a", attempt_number=1, plan=plan
    )
    progress = progress.begin(1).observe(
        1,
        handoff_id=RadioLocalHandoff(
            ordinal=1,
            native_message_id="packet-a",
            native_channel_id="0",
            confirmation_level="local_transport",
            payload_fingerprint=plain_channel_payload_fingerprint(0),
        ).encode(),
    )
    return provenance, progress


def test_retained_prefix_can_be_reviewed_but_never_grants_submission():
    claim, progress = _retained()
    report = assess_radio_continuation(StoredRadioAttempt(claim, progress, 2))
    assert report["status"] == "review_candidate"
    assert report["accepted_parts"] == 1
    assert report["unsent_parts"] == len(progress.parts) - 1
    assert report["native_submission_authorized"] is False
    assert "independent_new_outbox_claim_and_current_worker_lease" in report["requires"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("inflight", "uncertain_native_submission"),
        ("unknown", "uncertain_native_submission"),
        ("malformed", "invalid_accepted_handoff"),
        ("legacy", "missing_native_payload_proof"),
        ("mismatch", "inconsistent_native_payload_proof"),
        ("complete", "no_unsent_tail"),
        ("wrong_scope", "native_channel_payload_mismatch"),
        ("non_numeric_scope", "unsupported_native_channel_scope"),
        ("empty", "no_accepted_prefix"),
    ],
)
def test_uncertain_or_unproved_tail_is_held(change: str, reason: str):
    claim, progress = _retained()
    if change == "non_numeric_scope":
        claim = replace(claim, target_channel="room-channel")
    if change == "empty":
        progress = RadioAttemptProgress.bind(
            outbox_id=claim.outbox_id,
            attempt_number=claim.attempt_number,
            plan=progress.plan,
        )
    elif change in {"inflight", "unknown"}:
        progress = progress.begin(2)
        if change == "unknown":
            progress = progress.observe(2)
    elif change == "wrong_scope":
        progress = replace(
            progress,
            parts=(
                replace(
                    progress.parts[0],
                    handoff_id=RadioLocalHandoff(
                        ordinal=1,
                        native_message_id="packet-a",
                        native_channel_id="0",
                        confirmation_level="local_transport",
                        payload_fingerprint="b" * 64,
                    ).encode(),
                ),
                *progress.parts[1:],
            ),
        )
    elif change in {"malformed", "legacy"}:
        bad = (
            "not-json"
            if change == "malformed"
            else RadioLocalHandoff(
                ordinal=1,
                native_message_id="packet-a",
                native_channel_id="0",
                confirmation_level="local_transport",
            ).encode()
        )
        progress = replace(
            progress,
            parts=(replace(progress.parts[0], handoff_id=bad), *progress.parts[1:]),
        )
    elif change in {"mismatch", "complete"}:
        for ordinal in range(2, len(progress.parts) + 1 if change == "complete" else 3):
            progress = progress.begin(ordinal).observe(
                ordinal,
                handoff_id=RadioLocalHandoff(
                    ordinal=ordinal,
                    native_message_id=f"packet-{ordinal}",
                    native_channel_id="0",
                    confirmation_level="local_transport",
                    payload_fingerprint=(
                        "b" * 64
                        if change == "mismatch"
                        else plain_channel_payload_fingerprint(0)
                    ),
                ).encode(),
            )
    snapshot = StoredRadioAttempt(
        claim,
        progress,
        sum(
            0 if part.state == "unsent" else 1 if part.state == "submitting" else 2
            for part in progress.parts
        ),
    )
    report = assess_radio_continuation(snapshot)
    assert report["status"] == "held"
    assert reason in report["reasons"]
    assert report["native_submission_authorized"] is False


async def test_inspect_assessment_preserves_database_and_matches_schema(
    tmp_path, capsys
):
    """Operator report is closed-schema JSON and never performs recovery or submission."""
    path = tmp_path / "radio.sqlite"
    storage = SQLiteStorage(db_path=str(path))
    await storage.initialize()
    item = replace(make_outbox_item(status="in_progress"), worker_id="owner")
    item = await create_outbox_item_with_parent(storage, item)
    provenance = DeliveryAttemptProvenance(
        item.event_id,
        item.delivery_plan_id,
        item.target_adapter,
        item.target_channel,
        item.outbox_id,
        item.attempt_number,
        "live",
    )
    planned = plan_radio_text("hello " * 24, max_text_bytes=32)
    checkpoint = await storage.bind_radio_attempt(provenance, "owner", planned)
    assert checkpoint is not None
    submitting = await storage.begin_radio_part(checkpoint, "owner", 1)
    assert submitting is not None
    await storage.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    await _inspect_radio_attempt(
        item.outbox_id,
        item.attempt_number,
        storage_path=str(path),
        assess_continuation=True,
    )
    report = json.loads(capsys.readouterr().out)
    assert report["continuation_assessment"]["status"] == "held"
    assert "uncertain_native_submission" in report["continuation_assessment"]["reasons"]
    assert report["progress"]["parts"][0]["state"] == "submitting"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    folder = Path(__file__).resolve().parents[1] / "docs/schemas"
    registry = Registry()
    for name in (
        "radio-text-plan",
        "radio-attempt-progress",
        "radio-attempt-inspection",
    ):
        schema = json.loads((folder / f"{name}.schema.json").read_text())
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    schema = json.loads((folder / "radio-attempt-inspection.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, registry=registry).validate(report)


def test_cli_accepts_explicit_readonly_assessment_flag():
    """A malformed storage path is never opened by argument parsing."""
    from medre.cli.main import _build_parser

    args = _build_parser(include_adapter_cli=False).parse_args(
        [
            "inspect",
            "radio-attempt",
            "outbox-example",
            "2",
            "--storage-path",
            "/unopened/example.sqlite",
            "--assess-continuation",
        ]
    )
    assert args.assess_continuation is True
    assert args.outbox_id == "outbox-example" and args.attempt == 2


def test_inspection_and_production_share_exact_plain_channel_fingerprint():
    """A renderer and a retained native handoff agree on channel proof bytes."""
    from medre.core.engine.pipeline.radio_delivery import (
        _continuation_payload_fingerprint,
    )
    from medre.core.rendering.renderer import RenderingResult

    rendered = RenderingResult(
        event_id="event-a",
        target_adapter="meshcore",
        target_channel="0",
        payload={"text": "example", "channel_index": 0},
    )
    expected = hashlib.sha256(b'{"channel_index":0}').hexdigest()
    assert plain_channel_payload_fingerprint(0) == expected
    assert _continuation_payload_fingerprint(rendered) == expected


@pytest.mark.parametrize("native_channel", ["9", "", "0", None])
def test_retained_native_channel_scope_matches_declared_target(native_channel):
    """An absent channel differs from an explicit conflict with the retained scope."""
    claim, progress = _retained()
    conflicting = RadioLocalHandoff(
        ordinal=1,
        native_message_id="packet-a",
        native_channel_id=native_channel,
        confirmation_level="local_transport",
        payload_fingerprint=plain_channel_payload_fingerprint(0),
    )
    progress = replace(
        progress,
        parts=(
            replace(progress.parts[0], handoff_id=conflicting.encode()),
            *progress.parts[1:],
        ),
    )
    report = assess_radio_continuation(StoredRadioAttempt(claim, progress, 2))
    conflicting = native_channel not in {"0", None}
    assert report["status"] == ("held" if conflicting else "review_candidate")
    assert ("native_channel_scope_mismatch" in report["reasons"]) is conflicting
    assert report["native_submission_authorized"] is False
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/radio-attempt-inspection.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(
        schema["properties"]["continuation_assessment"]
    ).validate(report)
