"""mmrelay wire-format protocol constants and helpers.

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

# MEDRE extension: carries the structured reaction symbol (emoji) through the
# MMRelay emote-fallback path so the codec can recover the exact key without
# parsing the human-readable body.  Not a standard MMRelay wire key.
KEY_REACTION_KEY = "meshtastic_reaction_key"

# Protocol values
PORTNUM_TEXT = "TEXT_MESSAGE_APP"
EMOJI_FLAG_VALUE: int = 1


def derive_meshnet_value(
    source_origin_label: str | None,
    adapter_origin_label: str | None = None,
) -> str:
    """Derive the value for ``KEY_MESHNET`` from generic origin labels.

    Resolution precedence:
    1. *source_origin_label* (route/context level) when non-empty.
    2. *adapter_origin_label* (source-attribution registry) when non-empty.
    3. Empty string (neutral default).

    Parameters
    ----------
    source_origin_label:
        Route/context origin label (highest precedence).
    adapter_origin_label:
        Adapter-level origin label from the source_attribution registry.

    Returns
    -------
    str
        The string value to assign to ``KEY_MESHNET``.
    """
    if source_origin_label:
        return source_origin_label
    if adapter_origin_label:
        return adapter_origin_label
    return ""
