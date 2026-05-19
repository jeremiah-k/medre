"""Tests for mmrelay wire-format protocol constants."""

from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REPLY_ID,
    KEY_SHORTNAME,
    KEY_TEXT,
    PORTNUM_TEXT,
)


class TestMMRelayConstants:
    """Existing constants remain unchanged."""

    def test_key_id(self) -> None:
        assert KEY_ID == "meshtastic_id"

    def test_key_longname(self) -> None:
        assert KEY_LONGNAME == "meshtastic_longname"

    def test_key_shortname(self) -> None:
        assert KEY_SHORTNAME == "meshtastic_shortname"

    def test_key_meshnet(self) -> None:
        assert KEY_MESHNET == "meshtastic_meshnet"

    def test_key_portnum(self) -> None:
        assert KEY_PORTNUM == "meshtastic_portnum"

    def test_key_text(self) -> None:
        assert KEY_TEXT == "meshtastic_text"

    def test_portnum_text(self) -> None:
        assert PORTNUM_TEXT == "TEXT_MESSAGE_APP"


class TestMMRelayReplyReactionConstants:
    """New inbound relation semantics constants."""

    def test_key_reply_id(self) -> None:
        assert KEY_REPLY_ID == "meshtastic_replyId"

    def test_key_emoji(self) -> None:
        assert KEY_EMOJI == "meshtastic_emoji"

    def test_emoji_flag_value(self) -> None:
        assert EMOJI_FLAG_VALUE == 1
        assert isinstance(EMOJI_FLAG_VALUE, int)
