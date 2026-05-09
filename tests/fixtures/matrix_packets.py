"""Centralised Matrix nio-like event and response fixture factories.

These factories produce **duck-typed** approximations of the event and
response objects emitted by the `nio <https://github.com/poljar/matrix-nio>`_
Matrix client library.  They are pure-Python objects — no ``nio`` import is
required — that happen to carry the attribute shapes that ``MatrixAdapter``,
``MatrixCodec``, and ``MatrixRenderer`` expect.

Factory overview
----------------

Room events (duck-typed ``nio.RoomMessage*``):

* ``make_room_message`` — generic ``m.room.message`` with ``m.text`` msgtype.
* ``make_reply_event`` — room message carrying ``m.in_reply_to`` relation.
* ``make_self_message`` — room message whose sender matches the bot user.
* ``make_notice_message`` — room message with ``m.notice`` msgtype.
* ``make_emote_message`` — room message with ``m.emote`` msgtype.
* ``make_medre_envelope_message`` — room message with a MEDRE envelope in
  the content.
* ``make_corrupt_envelope_message`` — room message with a malformed MEDRE
  envelope (envelope is a string, not a dict).

Room / response objects (duck-typed ``nio.*Response``):

* ``make_room`` — duck-typed ``nio.MatrixRoom`` (has ``.room_id``).
* ``make_room_send_response`` — duck-typed ``nio.RoomSendResponse``
  (has ``.event_id``, ``.transport_response``).
* ``make_room_send_error`` — simulates a nio error response (no
  ``.event_id`` attribute; ``__str__`` returns the error message).

Usage::

    from tests.fixtures.matrix_packets import make_room_message

    event = make_room_message(sender="@alice:example.com", body="hello")
    assert event.sender == "@alice:example.com"
    assert event.source["content"]["msgtype"] == "m.text"
"""

from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_source(content: dict, event_id: str, sender: str) -> dict:
    """Construct a minimal nio-style ``.source`` dict.

    Parameters
    ----------
    content:
        The Matrix event content dict (includes ``msgtype``, ``body``, etc.).
    event_id:
        The Matrix event ID (e.g. ``$evt001``).
    sender:
        The fully-qualified Matrix user ID.
    """
    return {
        "type": "m.room.message",
        "content": content,
        "event_id": event_id,
        "sender": sender,
    }


# ---------------------------------------------------------------------------
# Room events
# ---------------------------------------------------------------------------
# Derivation: nio duck-typed — these factories produce objects whose
# attribute shape mirrors ``nio.RoomMessageText``, ``nio.RoomMessageNotice``,
# and ``nio.RoomMessageEmote`` as consumed by ``MatrixAdapter._on_room_message``
# and ``MatrixCodec``.
# ---------------------------------------------------------------------------


def make_room_message(
    sender: str = "@alice:example.com",
    event_id: str = "$evt001",
    body: str = "hello",
    content: dict | None = None,
    source: dict | None = None,
) -> SimpleNamespace:
    """Return a duck-typed ``nio.RoomMessageText`` event.

    If *content* is ``None`` it is derived from *body* with
    ``msgtype="m.text"``.  If *source* is ``None`` it is built from *content*,
    *event_id*, and *sender*.

    Parameters
    ----------
    sender:
        Fully-qualified Matrix user ID of the sender.
    event_id:
        Matrix event ID string.
    body:
        Plain-text body of the message.
    content:
        Override for the ``content`` dict.  Defaults to ``{"msgtype":
        "m.text", "body": body}``.
    source:
        Override for the ``source`` dict.  Defaults to a full event source
        built from *content*, *event_id*, and *sender*.
    """
    if content is None:
        content = {"msgtype": "m.text", "body": body}
    if source is None:
        source = _build_source(content, event_id, sender)
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source=source,
    )


def make_reply_event(
    original_event_id: str = "$orig001",
    sender: str = "@alice:example.com",
    event_id: str = "$reply001",
    body: str = "a reply",
) -> SimpleNamespace:
    """Return a room message carrying an ``m.in_reply_to`` relation.

    The content dict includes ``"m.relates_to"`` with the reply metadata.
    The source dict mirrors the full content.

    Parameters
    ----------
    original_event_id:
        The event ID being replied to.
    sender:
        Fully-qualified Matrix user ID of the sender.
    event_id:
        Matrix event ID for the reply itself.
    body:
        Plain-text body of the reply.
    """
    content = {
        "msgtype": "m.text",
        "body": body,
        "m.relates_to": {
            "m.in_reply_to": {"event_id": original_event_id},
        },
    }
    source = _build_source(content, event_id, sender)
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source=source,
    )


def make_self_message(
    user_id: str = "@bot:example.com",
    body: str = "self message",
) -> SimpleNamespace:
    """Return a room message whose sender equals *user_id*.

    Useful for testing that the adapter ignores messages from itself.

    Parameters
    ----------
    user_id:
        The bot's own Matrix user ID (used as both sender and identifier).
    body:
        Plain-text body of the message.
    """
    return make_room_message(sender=user_id, body=body)


def make_notice_message(
    sender: str = "@alice:example.com",
    event_id: str = "$notice001",
    body: str = "notice",
) -> SimpleNamespace:
    """Return a duck-typed ``nio.RoomMessageNotice`` event.

    Same shape as :func:`make_room_message` but with ``msgtype="m.notice"``.

    Parameters
    ----------
    sender:
        Fully-qualified Matrix user ID of the sender.
    event_id:
        Matrix event ID string.
    body:
        Plain-text body of the notice.
    """
    content = {"msgtype": "m.notice", "body": body}
    source = _build_source(content, event_id, sender)
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source=source,
    )


def make_emote_message(
    sender: str = "@alice:example.com",
    event_id: str = "$emote001",
    body: str = "emote",
) -> SimpleNamespace:
    """Return a duck-typed ``nio.RoomMessageEmote`` event.

    Same shape as :func:`make_room_message` but with ``msgtype="m.emote"``.

    Parameters
    ----------
    sender:
        Fully-qualified Matrix user ID of the sender.
    event_id:
        Matrix event ID string.
    body:
        Plain-text body of the emote.
    """
    content = {"msgtype": "m.emote", "body": body}
    source = _build_source(content, event_id, sender)
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source=source,
    )


# ---------------------------------------------------------------------------
# MEDRE envelope messages
# ---------------------------------------------------------------------------
# Derivation: synthetic scaffold — room messages carrying a MEDRE envelope
# in the content dict, used to exercise envelope parsing / validation paths.
# ---------------------------------------------------------------------------


def make_medre_envelope_message(
    source_adapter: str = "matrix-1",
    canonical_event_id: str = "evt-orig",
    sender: str = "@alice:example.com",
) -> SimpleNamespace:
    """Return a room message with a valid MEDRE envelope in the content.

    The content dict includes a ``"medre"`` key with an ``"envelope"`` dict
    containing ``source_adapter``, ``canonical_event_id``, and
    ``schema_version``.

    Parameters
    ----------
    source_adapter:
        Identifier of the originating adapter.
    canonical_event_id:
        The original event ID carried in the envelope.
    sender:
        Fully-qualified Matrix user ID of the sender.
    """
    content = {
        "msgtype": "m.text",
        "body": "medre msg",
        "medre": {
            "envelope": {
                "source_adapter": source_adapter,
                "canonical_event_id": canonical_event_id,
                "schema_version": 1,
            },
        },
    }
    source = _build_source(content, "$medre001", sender)
    return SimpleNamespace(
        sender=sender,
        event_id="$medre001",
        body="medre msg",
        source=source,
    )


def make_corrupt_envelope_message() -> SimpleNamespace:
    """Return a room message with a malformed MEDRE envelope.

    The ``medre.envelope`` value is a plain string instead of a dict,
    exercising error-handling paths in envelope validation.
    """
    content = {
        "msgtype": "m.text",
        "body": "corrupt",
        "medre": {"envelope": "not a dict"},
    }
    source = _build_source(content, "$corrupt001", "@alice:example.com")
    return SimpleNamespace(
        sender="@alice:example.com",
        event_id="$corrupt001",
        body="corrupt",
        source=source,
    )


# ---------------------------------------------------------------------------
# Room / response objects
# ---------------------------------------------------------------------------
# Derivation: nio duck-typed — objects whose attribute shape mirrors
# ``nio.MatrixRoom`` and ``nio.RoomSendResponse``.
# ---------------------------------------------------------------------------


def make_room(room_id: str = "!room:server") -> SimpleNamespace:
    """Return a duck-typed ``nio.MatrixRoom`` with ``.room_id``.

    Parameters
    ----------
    room_id:
        The Matrix room ID (e.g. ``!room:server``).
    """
    return SimpleNamespace(room_id=room_id)


def make_room_send_response(event_id: str = "$evt001") -> SimpleNamespace:
    """Return a duck-typed ``nio.RoomSendResponse``.

    Parameters
    ----------
    event_id:
        The event ID returned by the homeserver after a successful send.
    """
    return SimpleNamespace(event_id=event_id, transport_response=None)


def make_room_send_error(message: str = "Failed to send") -> object:
    """Return a duck-typed nio error response (no ``.event_id``).

    Simulates a failed ``room_send`` by producing an object that has **no**
    ``event_id`` attribute.  Includes a ``__str__`` method that returns the
    error message, matching nio's error response behaviour.

    Parameters
    ----------
    message:
        Human-readable error message.
    """

    class _ErrorResponse:
        """Minimal nio error-response stand-in."""

        def __init__(self, msg: str) -> None:
            self._message = msg

        def __str__(self) -> str:
            return self._message

    return _ErrorResponse(message)


def make_room_send_response_none_event_id() -> SimpleNamespace:
    """Return a response with ``event_id=None`` (malformed success).

    Simulates a homeserver bug where ``room_send`` returns a response
    object that has an ``event_id`` attribute but it is ``None``.
    """
    return SimpleNamespace(event_id=None, transport_response=None)


def make_room_send_response_empty_event_id() -> SimpleNamespace:
    """Return a response with ``event_id=""`` (malformed success).

    Simulates a homeserver bug where ``room_send`` returns a response
    object that has an ``event_id`` attribute but it is an empty string.
    """
    return SimpleNamespace(event_id="", transport_response=None)
