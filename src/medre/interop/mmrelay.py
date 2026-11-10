"""Legacy, explicitly opt-in MMRelay Matrix presentation compatibility.

These constants belong to a historical flat Matrix content format, **not** to
CanonicalEvent, the versioned Matrix-native metadata namespace, or the MEDRE
0.1.0 frozen schema. Shared-room peer relays use canonical source identity,
EventRelation, NativeRef and bounded bridge context instead.
"""

KEY_ID = "meshtastic_id"
KEY_LONGNAME = "meshtastic_longname"
KEY_SHORTNAME = "meshtastic_shortname"
KEY_MESHNET = "meshtastic_meshnet"
KEY_PORTNUM = "meshtastic_portnum"
KEY_TEXT = "meshtastic_text"
KEY_REPLY_ID = "meshtastic_replyId"
KEY_EMOJI = "meshtastic_emoji"

# Legacy output extension carrying the exact reaction symbol independently of
# presentation prose. Matrix ingress ignores it; canonical peers use the
# structured EventRelation.key in bounded bridge context.
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

    1. *source_origin_label* (route/context level) when **not** ``None``.
       An explicit empty string (``""``) is preserved — it means
       "intentionally blank/suppress label".
    2. *adapter_origin_label* (source-attribution registry) when **not**
       ``None``.
    3. Empty string (neutral default).

    ``None`` means "unset"; ``""`` means "explicitly empty".

    Parameters
    ----------
    source_origin_label:
        Route/context origin label (highest precedence).  ``""`` is
        treated as explicitly empty, not as unset.
    adapter_origin_label:
        Adapter-level origin label from the source_attribution registry.

    Returns
    -------
    str
        The string value to assign to ``KEY_MESHNET``.
    """
    if source_origin_label is not None:
        return source_origin_label
    if adapter_origin_label is not None:
        return adapter_origin_label
    return ""
