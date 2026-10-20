"""Durable plugin decisions: stage authority, identities, and closed serialization."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest

from medre.plugins import (
    Allow,
    Deny,
    Derive,
    DerivedEvent,
    Pass,
    PluginInstanceConfig,
    PluginStage,
    Supersede,
)
from medre.plugins.records import (
    MAX_PLUGIN_EMISSIONS,
    PluginExecutionRecord,
    PluginFailure,
    PluginStateSnapshot,
    canonical_json,
    configuration_digest,
    state_digest,
)
from tests.helpers.plugin_execution import make_record
from tests.helpers.plugin_spec import make_plugin_spec


@pytest.mark.parametrize(
    "stage,decision",
    [
        (PluginStage.ENRICH, Pass()),
        (
            PluginStage.ENRICH,
            Derive((DerivedEvent("plugin.custom", {"subtype": "one"}),)),
        ),
        (PluginStage.TRANSFORM, Pass()),
        (
            PluginStage.TRANSFORM,
            Derive((DerivedEvent("message.created", {"text": "add"}),)),
        ),
        (
            PluginStage.TRANSFORM,
            Supersede(DerivedEvent("message.created", {"text": "replace"})),
        ),
        (PluginStage.EVENT_POLICY, Allow()),
        (PluginStage.EVENT_POLICY, Deny("operator policy")),
    ],
)
def test_decisions_roundtrip_through_closed_schema(stage, decision) -> None:
    key = replace(make_record().key, stage=stage)
    record = make_record(key=key, decision=decision)
    data = json.loads(canonical_json(record.to_data()))
    assert PluginExecutionRecord.from_data(data) == record
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/plugin-execution-record.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(data)


@pytest.mark.parametrize(
    "stage,decision",
    [
        (PluginStage.ENRICH, Deny("no")),
        (PluginStage.ENRICH, Allow()),
        (PluginStage.ENRICH, Supersede(DerivedEvent("message.text"))),
        (PluginStage.TRANSFORM, Deny("no")),
        (PluginStage.EVENT_POLICY, Pass()),
        (PluginStage.EVENT_POLICY, Derive((DerivedEvent("plugin.custom"),))),
    ],
)
def test_cross_stage_authority_is_rejected(stage, decision) -> None:
    with pytest.raises(TypeError, match="not permitted"):
        make_record(key=replace(make_record().key, stage=stage), decision=decision)


@pytest.mark.parametrize("failure", list(PluginFailure))
def test_failures_roundtrip_and_classify_settlement(failure) -> None:
    record = make_record(
        decision=None, failure=failure, failure_reason="token=private unavailable"
    )
    assert "private" not in record.failure_reason
    assert record.settled == (
        failure
        in {PluginFailure.CONTRACT, PluginFailure.CAPABILITY, PluginFailure.QUARANTINED}
    )
    assert (
        PluginExecutionRecord.from_data(json.loads(canonical_json(record.to_data())))
        == record
    )


def test_enrichment_failure_can_explicitly_pass_without_inventing_output() -> None:
    record = make_record(
        failure=PluginFailure.TIMEOUT, failure_reason="deadline", decision=Pass()
    )
    assert record.settled
    assert record.produced_event_ids == ()


@pytest.mark.parametrize(
    "stage,decision",
    [
        (PluginStage.EVENT_POLICY, Allow()),
        (PluginStage.EVENT_POLICY, Deny("no")),
        (PluginStage.TRANSFORM, Pass()),
    ],
)
def test_failure_cannot_imply_policy_consent_or_transform_success(
    stage, decision
) -> None:
    with pytest.raises(ValueError, match="cannot invent"):
        make_record(
            key=replace(make_record().key, stage=stage),
            decision=decision,
            failure=PluginFailure.TIMEOUT,
            failure_reason="deadline",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"attempt": True},
        {"attempt": 0},
        {"plugin_version": ""},
        {"config_digest": "not-a-digest"},
        {"recorded_at": datetime(2026, 1, 1)},
        {"state_revision": -1},
        {"state_digest": "x"},
        {"decision": None},
        {"failure_reason": "unexpected"},
        {"decision": None, "failure": "timeout", "failure_reason": "deadline"},
        {"failure": PluginFailure.TIMEOUT, "failure_reason": " "},
    ],
)
def test_invalid_record_values_are_rejected(overrides) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_record(**overrides)


def test_output_count_and_record_size_are_bounded() -> None:
    with pytest.raises(ValueError, match="proposal limit"):
        make_record(
            decision=Derive(
                (DerivedEvent("plugin.custom"),) * (MAX_PLUGIN_EMISSIONS + 1)
            )
        )
    with pytest.raises(ValueError, match="record size"):
        make_record(
            decision=Derive((DerivedEvent("plugin.custom", {"large": "x" * 262144}),))
        )


def test_identity_excludes_version_and_config_but_separates_scope_instances_and_stages() -> (
    None
):
    first = make_record()
    changed = replace(first, plugin_version="2", config_digest="b" * 64)
    assert first.record_id == changed.record_id
    assert first.key.derived_event_id(0) == changed.key.derived_event_id(0)
    other_keys = [
        replace(first.key, instance_id="two"),
        replace(first.key, plugin_id="other"),
        replace(first.key, scope="replay:campaign"),
        replace(first.key, stage=PluginStage.TRANSFORM),
    ]
    assert (
        len(
            {
                first.key.derived_event_id(0),
                *(key.derived_event_id(0) for key in other_keys),
            }
        )
        == 5
    )
    assert first.key.derived_event_id(0) != first.key.derived_event_id(1)


@pytest.mark.parametrize("scope", ["", "replay:", "live ", "other", "replay: "])
def test_invalid_scopes_are_rejected(scope) -> None:
    with pytest.raises(ValueError, match="scope"):
        replace(make_record().key, scope=scope)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra=True),
        lambda data: data.update(produced_event_ids=["forged"]),
        lambda data: data["decision"].update(extra=True),
    ],
)
def test_corrupt_or_extended_evidence_is_rejected(mutation) -> None:
    data = make_record().to_data()
    mutation(data)
    with pytest.raises(ValueError, match="closed record contract"):
        PluginExecutionRecord.from_data(data)


def test_state_is_frozen_bounded_and_digested() -> None:
    values = {"cursor": [1]}
    snapshot = PluginStateSnapshot(1, values)
    values["cursor"].append(2)
    assert snapshot.values["cursor"] == (1,)
    with pytest.raises(ValueError, match="size limit"):
        PluginStateSnapshot(0, {"huge": "x" * 65536})
    assert state_digest({"a": 1, "b": 2}) == state_digest({"b": 2, "a": 1})


def test_effective_config_digest_is_order_independent_and_tracks_semantics() -> None:
    spec = make_plugin_spec("sensor")
    instance = PluginInstanceConfig(
        "sensor", "one", grants=spec.capabilities, config={"b": 2, "a": 1}
    )
    digest = configuration_digest(spec, instance)
    assert digest == configuration_digest(
        spec, replace(instance, config={"a": 1, "b": 2})
    )
    assert digest != configuration_digest(spec, replace(instance, grants=frozenset()))
    assert digest != configuration_digest(spec, replace(instance, config={"a": 2}))


def test_record_schema_tracks_the_serialized_contract() -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/plugin-execution-record.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    assert set(schema["properties"]) == set(make_record().to_data())
    assert set(schema["required"]) == set(make_record().to_data())


def test_schema_rejects_policy_failure_consent() -> None:
    record = make_record(
        key=replace(make_record().key, stage=PluginStage.EVENT_POLICY),
        decision=None,
        failure=PluginFailure.TIMEOUT,
        failure_reason="deadline",
    )
    data = json.loads(canonical_json(record.to_data()))
    data["decision"] = {"kind": "allow", "note": ""}
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/plugin-execution-record.schema.json"
        ).read_text()
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(data)


@pytest.mark.parametrize("offset", [-2, -1, 0, 1])
def test_long_redacted_failure_reasons_roundtrip(offset) -> None:
    from medre.core.observability.sanitization import (
        _MAX_ERROR_DETAIL_LEN,
        REDACTED_TOKEN,
        sanitize_error,
    )

    reason = (
        "!" * (_MAX_ERROR_DETAIL_LEN - len(REDACTED_TOKEN) + offset)
        + "token="
        + "secret" * 50
    )
    record = make_record(
        decision=None, failure=PluginFailure.TRANSIENT, failure_reason=reason
    )
    assert len(record.failure_reason) <= _MAX_ERROR_DETAIL_LEN
    assert "secret" not in record.failure_reason
    assert sanitize_error(record.failure_reason) == record.failure_reason
    assert replace(record) == record
    assert (
        PluginExecutionRecord.from_data(json.loads(canonical_json(record.to_data())))
        == record
    )


@pytest.mark.parametrize(
    "reason",
    [
        "!" * 500 + "token=" + " " * 100,
        "!" * 505 + "token=x",
        "!" * 499 + "password:" + " " * 100,
    ],
)
def test_failure_reason_truncation_cannot_create_an_unstable_secret_match(
    reason,
) -> None:
    from medre.core.observability.sanitization import sanitize_error

    record = make_record(
        decision=None, failure=PluginFailure.TRANSIENT, failure_reason=reason
    )
    assert len(record.failure_reason) <= 512
    assert sanitize_error(record.failure_reason) == record.failure_reason
    assert (
        PluginExecutionRecord.from_data(json.loads(canonical_json(record.to_data())))
        == record
    )
