"""Offline checks of the locally serialized XMPP write evidence."""

import pytest

from tests.helpers.prosody import groupchat_write


@pytest.mark.parametrize("namespace", ["", ' xmlns="jabber:client"'])
@pytest.mark.parametrize("encode", [False, True])
def test_actual_destination_and_escaped_text_are_recorded(namespace, encode):
    raw = (
        f'<message{namespace} to="actual@rooms.test" type="groupchat">'
        "<body>A &amp; B &lt; C</body>"
        '<origin-id xmlns="urn:xmpp:sid:0" id="actual-origin"/>'
        "</message>"
    )
    assert groupchat_write(raw.encode() if encode else raw) == {
        "to": "actual@rooms.test",
        "body": "A & B < C",
        "origin_id": "actual-origin",
        "native_id": None,
    }


def test_non_groupchat_writes_are_not_delivery_evidence():
    assert groupchat_write("<presence/>") is None
    assert (
        groupchat_write('<message type="chat"><body>ignored</body></message>') is None
    )


def test_prefixed_groupchat_is_still_recorded():
    record = groupchat_write(
        '<xmpp:message xmlns:xmpp="jabber:client" type="groupchat">'
        "<xmpp:body>received</xmpp:body></xmpp:message>"
    )
    assert record is not None and record["body"] == "received"


def test_malformed_message_is_not_silently_discarded():
    with pytest.raises(ValueError, match="malformed serialized XMPP message"):
        groupchat_write('<message type="groupchat"><body>truncated')
