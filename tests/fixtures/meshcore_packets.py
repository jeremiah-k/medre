"""Centralised MeshCore packet fixture factories.

These factories produce plain-dict approximations of the event payload
structures emitted by MeshCore's CONTACT_MSG_RECV, CHANNEL_MSG_RECV,
and ACK events.  They are **MEDRE fixture approximations**, not exhaustive
captures — real MeshCore events may carry additional fields not represented
here.

MeshCore packet semantics
-------------------------
MeshCore uses a simpler event model than Meshtastic:

* **CONTACT_MSG_RECV** (DM): payload has ``text``, ``pubkey_prefix``,
  ``type``=``"PRIV"``, ``txt_type``, ``sender_timestamp``.
* **CHANNEL_MSG_RECV** (channel): payload has ``text``, ``channel_idx``,
  ``type``=``"CHAN"``, ``txt_type``, ``sender_timestamp``.
* **ACK**: payload has ``code``.

There is no portnum concept — MeshCore uses ``txt_type`` instead.

Usage::

    from tests.fixtures.meshcore_packets import make_contact_text_packet

    pkt = make_contact_text_packet(text="hello", sender_prefix="abc123")
"""


# ---------------------------------------------------------------------------
# Contact (DM) text packets
# ---------------------------------------------------------------------------
# Derivation: MEDRE synthetic — invented for MEDRE test coverage based on
# MeshCore's CONTACT_MSG_RECV event shape.
# ---------------------------------------------------------------------------

def make_contact_text_packet(
    text: str = "hello from meshcore",
    sender_prefix: str = "abc123",
    timestamp: int = 42,
    txt_type: int = 0,
) -> dict:
    """Return a CONTACT_MSG_RECV (DM) text packet dict.

    Parameters
    ----------
    text:
        Message body text.
    sender_prefix:
        Sender's pubkey prefix.
    timestamp:
        Sender timestamp (used as packet_id).
    txt_type:
        MeshCore text type indicator.
    """
    return {
        "text": text,
        "pubkey_prefix": sender_prefix,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": txt_type,
    }


# ---------------------------------------------------------------------------
# Channel text packets
# ---------------------------------------------------------------------------
# Derivation: MEDRE synthetic — invented for MEDRE test coverage based on
# MeshCore's CHANNEL_MSG_RECV event shape.
# ---------------------------------------------------------------------------

def make_channel_text_packet(
    text: str = "hello channel",
    channel_idx: int = 0,
    timestamp: int = 42,
    txt_type: int = 0,
) -> dict:
    """Return a CHANNEL_MSG_RECV text packet dict.

    Parameters
    ----------
    text:
        Message body text.
    channel_idx:
        Channel index.
    timestamp:
        Sender timestamp (used as packet_id).
    txt_type:
        MeshCore text type indicator.
    """
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": txt_type,
        "pubkey_prefix": "chan_sender",
    }


# ---------------------------------------------------------------------------
# ACK packets
# ---------------------------------------------------------------------------
# Derivation: MEDRE synthetic — ACK events carry a ``code`` key.
# ---------------------------------------------------------------------------

def make_ack_packet(code: int = 0) -> dict:
    """Return an ACK packet dict.

    Parameters
    ----------
    code:
        ACK status code.
    """
    return {
        "code": code,
    }


# ---------------------------------------------------------------------------
# Edge-case / special variants
# ---------------------------------------------------------------------------

def make_minimal_packet() -> dict:
    """Bare-minimum packet — no ``text``, no ``channel_idx``.

    Useful for testing graceful degradation when a packet is missing
    expected keys.
    """
    return {}
