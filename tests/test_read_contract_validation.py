"""Typed scoped-read results reject inconsistent shapes at construction."""

import pytest

from medre.core.storage.read_contracts import (
    AuthorizedEventRead,
    ContentBodyRead,
    ReplyContextRead,
)


@pytest.mark.parametrize(
    "status",
    ["retained"],
)
def test_content_body_read_retained_requires_a_string_body(status) -> None:
    with pytest.raises(ValueError, match="Only retained reads contain a string body"):
        ContentBodyRead(status)
    with pytest.raises(ValueError, match="Only retained reads contain a string body"):
        ContentBodyRead(status, body=None)


def test_content_body_read_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="Unknown content body read status"):
        ContentBodyRead("expired")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["unavailable", "denied"])
def test_non_retained_body_reads_reject_any_body(status) -> None:
    with pytest.raises(ValueError, match="Only retained reads contain a string body"):
        ContentBodyRead(status, body="leaked")


def test_authorized_event_read_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="Unknown authorized event read status"):
        AuthorizedEventRead("borrowed", None)  # type: ignore[arg-type]


def test_authorized_event_read_retained_requires_an_event() -> None:
    with pytest.raises(ValueError, match="Only retained authorized reads"):
        AuthorizedEventRead("retained", None)


def test_authorized_event_read_denied_rejects_an_event() -> None:
    with pytest.raises(ValueError, match="Only retained authorized reads"):
        AuthorizedEventRead("denied", object())


def test_reply_context_read_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="Unknown reply context read status"):
        ReplyContextRead("partial", None)  # type: ignore[arg-type]


def test_reply_context_read_denied_rejects_sender_context() -> None:
    with pytest.raises(ValueError, match="Only authorized reply reads"):
        ReplyContextRead("denied", object())


@pytest.mark.parametrize("status", ["retained", "unavailable"])
def test_reply_context_read_authorized_variants_require_context(status) -> None:
    with pytest.raises(ValueError, match="Only authorized reply reads"):
        ReplyContextRead(status, None)
