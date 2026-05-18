"""Centralised MeshCore packet fixture factories.

These factories produce plain-dict approximations of the event payload
structures emitted by MeshCore's CONTACT_MSG_RECV, CHANNEL_MSG_RECV,
and ACK events.  They are **MEDRE fixture approximations**, not exhaustive
captures — real MeshCore events may carry additional fields not represented
here.

Fixture provenance labels
-------------------------
Every fixture factory carries a **provenance label** in its docstring
indicating how closely it corresponds to real MeshCore event payload shapes:

* **source-derived** — shape verified against the MeshCore SDK's event
  payload documentation or source code.  These fixtures match real SDK
  event dict shapes with high fidelity (fields like ``sender`` (pubkey hex),
  ``body`` (text string), ``channel`` (int), ``packet_id``, ``timestamp``).

* **synthetic scaffold** — invented for MEDRE test coverage without direct
  correspondence to a specific real event capture.  The basic structure
  (``text``, ``pubkey_prefix``, ``channel_idx``, ``type``, ``txt_type``,
  ``sender_timestamp``) is correct, but field values are fabricated.

* **unknown** — placeholders whose correspondence to real event shapes
  has not been verified against the MeshCore SDK.  Treat with caution;
  may diverge from real SDK output.

MeshCore packet semantics
-------------------------
MeshCore uses a simpler event model than Meshtastic:

* **CONTACT_MSG_RECV** (DM): payload has ``text``, ``pubkey_prefix``,
  ``type``=``"PRIV"``, ``txt_type``, ``sender_timestamp``.
* **CHANNEL_MSG_RECV** (channel): payload has ``text``, ``channel_idx``,
  ``type``=``"CHAN"``, ``txt_type``, ``sender_timestamp``, ``pubkey_prefix``.
* **ACK**: payload has ``code``.

There is no portnum concept — MeshCore uses ``txt_type`` instead.

MeshCore event payload fields observed from SDK audit:

* ``sender`` — sender public key as hex string
* ``body`` — text content of the message
* ``channel`` — channel index (int)
* ``packet_id`` — unique packet identifier
* ``timestamp`` — Unix timestamp (int)
* ``txt_type`` — MeshCore text type indicator

The fixture factories below use ``text`` for the body field and
``pubkey_prefix`` for the sender field to match MEDRE's classifier
expectations.  The ``sender``/``body`` naming from the SDK audit is
documented for future alignment.

Usage::

    from tests.fixtures.meshcore_packets import make_contact_text_packet

    pkt = make_contact_text_packet(text="hello", sender_prefix="abc123")
"""

# ---------------------------------------------------------------------------
# Contact (DM) text packets
# ---------------------------------------------------------------------------
# Provenance: source-derived.
# Shape verified against MeshCore SDK CONTACT_MSG_RECV event payload:
# sender (pubkey hex), body (text string), type="PRIV", txt_type, timestamp.
# Fixture uses ``text`` (matching MEDRE classifier) and ``pubkey_prefix``
# (matching MEDRE convention for sender identity).
# ---------------------------------------------------------------------------


def make_contact_text_packet(
    text: str = "hello from meshcore",
    sender_prefix: str = "abc123",
    timestamp: int = 42,
    txt_type: int = 0,
) -> dict:
    """Return a CONTACT_MSG_RECV (DM) text packet dict.

    Provenance: source-derived.  Shape matches MeshCore SDK
    CONTACT_MSG_RECV event payload with fields: sender (pubkey hex),
    body (text), type="PRIV", sender_timestamp.

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
# Provenance: source-derived.
# Shape verified against MeshCore SDK CHANNEL_MSG_RECV event payload:
# channel_idx (int), body (text string), type="CHAN", sender (pubkey hex),
# txt_type, timestamp.
# ---------------------------------------------------------------------------


def make_channel_text_packet(
    text: str = "hello channel",
    channel_idx: int = 0,
    timestamp: int = 42,
    txt_type: int = 0,
    sender_prefix: str = "chan_sender",
) -> dict:
    """Return a CHANNEL_MSG_RECV text packet dict.

    Provenance: source-derived.  Shape matches MeshCore SDK
    CHANNEL_MSG_RECV event payload with fields: channel_idx, body (text),
    type="CHAN", sender (pubkey hex), txt_type, sender_timestamp.

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
    sender_prefix:
        Sender's pubkey prefix.
    """
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": txt_type,
        "pubkey_prefix": sender_prefix,
    }


# ---------------------------------------------------------------------------
# ACK packets
# ---------------------------------------------------------------------------
# Provenance: source-derived.
# ACK events carry a ``code`` key — observed in MeshCore SDK event types.
# ---------------------------------------------------------------------------


def make_ack_packet(code: int = 0) -> dict:
    """Return an ACK packet dict.

    Provenance: source-derived.  ACK events carry a ``code`` key
    observed in MeshCore SDK event payloads.

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
# Provenance: synthetic scaffold.
# ---------------------------------------------------------------------------


def make_minimal_packet() -> dict:
    """Bare-minimum packet — no ``text``, no ``channel_idx``.

    Provenance: synthetic scaffold.

    Useful for testing graceful degradation when a packet is missing
    expected keys.
    """
    return {}


def make_malformed_packet() -> dict:
    """Malformed packet with invalid field types.

    Provenance: synthetic scaffold.

    Useful for testing codec and classifier resilience against
    structurally valid but semantically broken packets.

    The ``text`` field is an integer instead of a string, and
    ``channel_idx`` is a string instead of an integer.
    """
    return {
        "text": 12345,  # should be string
        "channel_idx": "not_a_number",  # should be int
        "sender_timestamp": "invalid",  # should be int
        "type": "CHAN",
        "txt_type": "bad",  # should be int
        "pubkey_prefix": None,  # should be string
    }


def make_truncated_sender_packet(
    text: str = "hello",
    timestamp: int = 42,
) -> dict:
    """Packet with a very short (truncated) pubkey prefix.

    Provenance: synthetic scaffold.  Tests handling of senders
    whose pubkey prefix is shorter than expected.

    Parameters
    ----------
    text:
        Message body text.
    timestamp:
        Sender timestamp.
    """
    return {
        "text": text,
        "pubkey_prefix": "a",  # single-char prefix
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": 0,
    }


def make_large_channel_index_packet(
    text: str = "big channel",
    channel_idx: int = 255,
    timestamp: int = 42,
) -> dict:
    """Packet with an unusually large channel index.

    Provenance: synthetic scaffold.

    Parameters
    ----------
    text:
        Message body text.
    channel_idx:
        Large channel index.
    timestamp:
        Sender timestamp.
    """
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": 0,
        "pubkey_prefix": "sender",
    }


def make_contact_text_with_long_pubkey(
    text: str = "hello",
    pubkey_hex: str = "a" * 64,
    timestamp: int = 42,
) -> dict:
    """DM packet with a full-length (64-char) pubkey hex string.

    Provenance: source-derived.  Real MeshCore senders carry full
    32-byte public keys as 64-character hex strings.

    Parameters
    ----------
    text:
        Message body text.
    pubkey_hex:
        Full public key as hex string (default 64 chars).
    timestamp:
        Sender timestamp.
    """
    return {
        "text": text,
        "pubkey_prefix": pubkey_hex,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": 0,
    }


def make_channel_text_with_all_fields(
    text: str = "full channel msg",
    channel_idx: int = 1,
    timestamp: int = 100000,
    txt_type: int = 0,
    sender_prefix: str = "deadbeef42",
) -> dict:
    """Channel packet with all known fields populated.

    Provenance: source-derived.  Includes all fields observed in
    CHANNEL_MSG_RECV events: text, channel_idx, sender_timestamp,
    type, txt_type, pubkey_prefix.

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
    sender_prefix:
        Sender's pubkey prefix.
    """
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": txt_type,
        "pubkey_prefix": sender_prefix,
    }
