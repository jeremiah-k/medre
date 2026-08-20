"""Installed mindroom-nio decrypted-event SDK contract guard.

These tests pin the shape produced by the pinned mindroom-nio fork so the
Matrix session boundary keeps a stable contract for decrypted-event
registration. They exercise the same SDK objects the production session
consumes at the boundary, so any change in the fork's
``Event.parse_decrypted_event`` / ``RoomMessage.parse_decrypted_event``
or ``RedactionEvent.from_dict`` shape is caught here.

The ``matrix_sdk`` marker deselects this module from the default suite
because it requires the pinned ``mindroom-nio`` SDK. It runs only when
operators explicitly enable the marker.
"""

from __future__ import annotations

import pytest


@pytest.mark.matrix_sdk
def test_decrypted_source_carries_inner_event_type() -> None:
    """Decrypted event ``.source`` carries the inner Matrix event type.

    ``Event.parse_decrypted_event`` parses the megolm plaintext (which
    carries the INNER event ``type``) into ``parsed_dict`` and passes
    it to ``Event.parse_decrypted_event``; the resulting event's
    ``.source`` therefore carries the inner type. When the inner
    payload is an encrypted media ``m.room.message`` with a ``file``
    key, ``RoomMessage.parse_decrypted_event`` produces a
    ``RoomEncryptedImage`` subclass.
    """
    import nio

    d = {
        "type": "m.room.message",
        "event_id": "$evt-1",
        "sender": "@alice:example.com",
        "origin_server_ts": 1_700_000_000_000,
        "room_id": "!room:example.com",
        "content": {
            "msgtype": "m.image",
            "body": "photo.jpg",
            "file": {
                "url": "mxc://example.com/media",
                "hashes": {"sha256": "abcd"},
                "iv": "ivbytes",
                "key": {"alg": "A256CTR", "k": "keybytes"},
            },
        },
    }

    event = nio.Event.parse_decrypted_event(d)

    assert isinstance(event, nio.RoomEncryptedImage)
    assert event.source["type"] == "m.room.message"


@pytest.mark.matrix_sdk
def test_redaction_source_preserves_redacts() -> None:
    """RedactionEvent ``.source`` preserves top-level ``redacts``.

    ``RedactionEvent.from_dict`` reads ``redacts`` from top level or
    content.  When the source dict carries ``redacts`` at the top
    level, ``.source`` carries both ``type`` and ``redacts`` verbatim
    so downstream consumers can resolve the redacted target without
    re-reading the dict.
    """
    import nio

    d = {
        "type": "m.room.redaction",
        "event_id": "$re-1",
        "sender": "@alice:example.com",
        "origin_server_ts": 1_700_000_000_000,
        "room_id": "!room:example.com",
        "redacts": "$target",
        "content": {"reason": "cleanup"},
    }

    event = nio.RedactionEvent.from_dict(d)

    assert event.source["type"] == "m.room.redaction"
    assert event.source["redacts"] == "$target"


@pytest.mark.matrix_sdk
def test_event_classes_discoverable_for_session_registration() -> None:
    """Session-boundary class discovery finds the expected Matrix events.

    The session registers reaction / redaction / media event classes
    via the ``_reaction_event_classes`` / ``_redaction_event_classes``
    / ``_media_message_classes`` discoverers. This test verifies the
    discoverers still find the SDK classes when running against the
    pinned fork.
    """
    import nio

    from medre.adapters.matrix.session import (
        _media_message_classes,
        _reaction_event_classes,
        _redaction_event_classes,
    )

    media = _media_message_classes(nio)
    redactions = _redaction_event_classes(nio)
    reactions = _reaction_event_classes(nio)

    assert media, "media message classes discovery returned empty"
    assert redactions, "redaction classes discovery returned empty"
    assert reactions, "reaction classes discovery returned empty"

    # The discoverers dedupe across export locations; every returned
    # class is therefore unique.
    assert len(set(redactions)) == len(redactions)
    assert len(set(reactions)) == len(reactions)

    # Redaction / reaction discoverers must resolve to their target
    # SDK class — at least one match each.
    assert any(cls is nio.RedactionEvent for cls in redactions)
    assert any(cls is nio.ReactionEvent for cls in reactions)

    # Media discovery must surface at least four unique classes
    # (image / audio / video / file in either encrypted or plaintext
    # form) including at least one RoomEncrypted* subclass.
    assert len(set(media)) >= 4
    assert any(cls is nio.RoomEncryptedImage for cls in media)
