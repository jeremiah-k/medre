"""The published descriptor schema matches admission's strict metadata seam."""

import json
from pathlib import Path

import jsonschema
import pytest

from medre.core.events.attachments import (
    ATTACHMENT_KINDS,
    ATTACHMENT_UNAVAILABLE_REASONS,
    AttachmentDescriptor,
    AttachmentDescriptorError,
    declared_descriptor_from_event_payload,
)

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1] / "docs/schemas/attachment.schema.json"
    ).read_text()
)
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
REF = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "file", "content_ref": REF, "size_bytes": 0},
        {
            "kind": "image",
            "width": 5,
            "height": 7,
            "filename": "photo.png",
            "mime_type": "image/png",
            "unavailable_reason": "not_retained",
        },
        {
            "kind": "audio",
            "duration_ms": 3,
            "content_ref": REF,
            "unavailable_reason": None,
        },
        {
            "kind": "video",
            "content_ref": None,
            "unavailable_reason": "policy_disabled",
            "width": None,
        },
        {"kind": "file"},
        {"kind": "file", "content_ref": REF, "unavailable_reason": "not_retained"},
        {"kind": "file", "content_ref": REF + "\n"},
        {"kind": "file", "content_ref": "https://private.example/key"},
        {"kind": "file", "content_ref": REF, "size_bytes": True},
        {"kind": "file", "content_ref": REF, "height": -1},
        {"kind": "file", "content_ref": REF, "filename": ""},
        {"kind": "file", "content_ref": REF, "caption": "unsupported extension"},
        {"kind": "file", "unavailable_reason": "unknown"},
    ],
)
def test_persisted_schema_and_descriptor_validator_agree(payload):
    accepted = VALIDATOR.is_valid(payload)
    try:
        descriptor = AttachmentDescriptor.from_payload(payload)
    except AttachmentDescriptorError:
        assert not accepted
    else:
        assert accepted
        VALIDATOR.validate(descriptor.to_payload())


def test_attachment_schema_tracks_all_fields_and_closed_vocabularies():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert set(SCHEMA["properties"]) == set(AttachmentDescriptor.__slots__)
    assert set(SCHEMA["properties"]["kind"]["enum"]) == ATTACHMENT_KINDS
    assert (
        set(SCHEMA["properties"]["unavailable_reason"]["enum"]) - {None}
        == ATTACHMENT_UNAVAILABLE_REASONS
    )


def test_declared_descriptor_is_valid_only_before_retention():
    payload = {"kind": "image", "filename": "photo.png", "width": 50}
    declared = SCHEMA["$defs"]["DeclaredAttachment"]
    jsonschema.validate(payload, declared)
    descriptor = declared_descriptor_from_event_payload({"attachment": payload})
    assert descriptor is not None
    assert descriptor.to_declared_payload() == payload
    assert not VALIDATOR.is_valid(payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**payload, "content_ref": REF}, declared)
