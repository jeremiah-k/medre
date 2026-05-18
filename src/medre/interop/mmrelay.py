"""mmrelay wire-format protocol constants.

These key names are dictated by the mmrelay project's Matrix message schema.
They live outside any adapter package because they define a cross-adapter
wire contract, not an implementation detail of any single adapter.
"""

KEY_ID = "meshtastic_id"
KEY_LONGNAME = "meshtastic_longname"
KEY_SHORTNAME = "meshtastic_shortname"
KEY_MESHNET = "meshtastic_meshnet"
KEY_PORTNUM = "meshtastic_portnum"
KEY_TEXT = "meshtastic_text"
KEY_REPLY_ID = "meshtastic_replyId"
KEY_EMOJI = "meshtastic_emoji"

# Protocol values
PORTNUM_TEXT = "TEXT_MESSAGE_APP"
EMOJI_FLAG_VALUE: int = 1
