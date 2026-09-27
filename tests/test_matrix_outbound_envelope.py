"""Closed-envelope contract tests for ``MatrixOutboundOperation``.

Every validation branch of the outbound envelope is pinned here: unknown
kinds, per-kind field exclusivity, the ``send_media`` content template
restrictions (no bytes, no key material, no pre-existing url/file), and
strict payload decoding.
"""

from __future__ import annotations

import pytest

from medre.adapters.matrix.outbound import (
    MATRIX_OPERATION_KEY,
    MatrixOutboundEnvelopeError,
    MatrixOutboundOperation,
)

CONTENT_REF = "sha256:" + "a" * 64
MEDIA_TEMPLATE: dict[str, object] = {"msgtype": "m.file", "body": "f.bin"}


def test_unknown_operation_kind_raises() -> None:
    with pytest.raises(MatrixOutboundEnvelopeError, match="unknown operation kind"):
        MatrixOutboundOperation(kind="phoning_home")


def test_send_event_must_not_carry_redaction_or_media_fields() -> None:
    content = {"body": "hi"}
    with pytest.raises(MatrixOutboundEnvelopeError, match="redacts_event_id"):
        MatrixOutboundOperation(
            kind="send_event",
            event_type="m.room.message",
            content=content,
            redacts_event_id="$x",
        )
    with pytest.raises(MatrixOutboundEnvelopeError, match="reason"):
        MatrixOutboundOperation(
            kind="send_event",
            event_type="m.room.message",
            content=content,
            reason="why",
        )
    with pytest.raises(MatrixOutboundEnvelopeError, match="content_ref"):
        MatrixOutboundOperation(
            kind="send_event",
            event_type="m.room.message",
            content=content,
            content_ref=CONTENT_REF,
        )


def test_redact_event_requires_target_and_forbids_content_ref() -> None:
    with pytest.raises(MatrixOutboundEnvelopeError, match="redacts_event_id"):
        MatrixOutboundOperation(kind="redact_event")
    with pytest.raises(MatrixOutboundEnvelopeError, match="content_ref"):
        MatrixOutboundOperation(
            kind="redact_event", redacts_event_id="$x", content_ref=CONTENT_REF
        )


def test_send_media_forbids_send_and_redaction_fields() -> None:
    with pytest.raises(MatrixOutboundEnvelopeError, match="event_type"):
        MatrixOutboundOperation(
            kind="send_media",
            content_ref=CONTENT_REF,
            content=MEDIA_TEMPLATE,
            event_type="m.room.message",
        )
    with pytest.raises(MatrixOutboundEnvelopeError, match="redacts_event_id"):
        MatrixOutboundOperation(
            kind="send_media",
            content_ref=CONTENT_REF,
            content=MEDIA_TEMPLATE,
            redacts_event_id="$x",
        )
    with pytest.raises(MatrixOutboundEnvelopeError, match="reason"):
        MatrixOutboundOperation(
            kind="send_media",
            content_ref=CONTENT_REF,
            content=MEDIA_TEMPLATE,
            reason="why",
        )


def test_send_media_requires_valid_content_reference() -> None:
    for bad in (None, "", "mxc://hs/media", "sha256:xyz", "sha256:" + "A" * 64):
        with pytest.raises(MatrixOutboundEnvelopeError, match="content reference"):
            MatrixOutboundOperation(
                kind="send_media", content_ref=bad, content=MEDIA_TEMPLATE
            )


def test_send_media_requires_media_template_without_locator() -> None:
    with pytest.raises(MatrixOutboundEnvelopeError, match="non-empty wire template"):
        MatrixOutboundOperation(kind="send_media", content_ref=CONTENT_REF, content={})
    with pytest.raises(MatrixOutboundEnvelopeError, match="media msgtype"):
        MatrixOutboundOperation(
            kind="send_media",
            content_ref=CONTENT_REF,
            content={"msgtype": "m.text", "body": "hi"},
        )
    with pytest.raises(MatrixOutboundEnvelopeError, match="msgtype"):
        MatrixOutboundOperation(
            kind="send_media", content_ref=CONTENT_REF, content={"body": "hi"}
        )
    for locator in ("url", "file"):
        with pytest.raises(MatrixOutboundEnvelopeError, match=locator):
            MatrixOutboundOperation(
                kind="send_media",
                content_ref=CONTENT_REF,
                content={"msgtype": "m.file", locator: "mxc://hs/x"},
            )


def test_from_payload_roundtrip_and_passthrough() -> None:
    operation = MatrixOutboundOperation.send_media(CONTENT_REF, MEDIA_TEMPLATE)
    decoded = MatrixOutboundOperation.from_payload(operation.to_payload())
    assert decoded == operation
    # An already-decoded operation passes through unchanged.
    assert MatrixOutboundOperation.from_payload({MATRIX_OPERATION_KEY: operation}) is (
        operation
    )


def test_from_payload_absent_key_returns_none() -> None:
    assert MatrixOutboundOperation.from_payload({"other": 1}) is None


def test_from_payload_rejects_malformed_envelopes() -> None:
    with pytest.raises(MatrixOutboundEnvelopeError, match="must be a mapping"):
        MatrixOutboundOperation.from_payload("not a mapping")
    with pytest.raises(MatrixOutboundEnvelopeError, match="must be a mapping"):
        MatrixOutboundOperation.from_payload({MATRIX_OPERATION_KEY: "not a mapping"})
    with pytest.raises(MatrixOutboundEnvelopeError, match="unknown _matrix_operation"):
        MatrixOutboundOperation.from_payload(
            {MATRIX_OPERATION_KEY: {"kind": "send_event", "bogus": 1}}
        )
    with pytest.raises(MatrixOutboundEnvelopeError, match="invalid _matrix_operation"):
        MatrixOutboundOperation.from_payload({MATRIX_OPERATION_KEY: {"kind": 3}})
