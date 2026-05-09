"""Centralised LXMF packet fixture factories.

These factories produce plain-dict approximations of the message payload
structures carried by LXMF messages.  They are **MEDRE fixture
approximations**, not exhaustive captures — real LXMF messages may carry
additional fields not represented here.

LXMF message semantics
----------------------
LXMF (Lightweight Extensible Message Format) messages carry:

* ``source_hash``: 16-byte sender identity hash.
* ``destination_hash``: 16-byte recipient identity hash.
* ``message_id``: 32-byte SHA-256 hash (also called "hash").
* ``timestamp``: UNIX float seconds.
* ``title``: UTF-8 string (optional).
* ``content``: UTF-8 string body.
* ``fields``: Extensible metadata dict.
* ``signature_validated``: Whether Ed25519 signature was verified.
* ``has_fields``: Whether fields dict is non-empty.

Usage::

    from tests.fixtures.lxmf_packets import make_lxmf_text_packet

    pkt = make_lxmf_text_packet(content="hello", source_hash="ab"*16)
"""


# ---------------------------------------------------------------------------
# Text packets
# ---------------------------------------------------------------------------
# Derivation: LXMF-source-derived — based on the LXMF message payload
# shape with content, source_hash, message_id, and timestamp fields.
# ---------------------------------------------------------------------------


def make_lxmf_text_packet(
    content: str = "hello",
    source_hash: str = "ab" * 16,
    msg_id: str | None = None,
    timestamp: float | None = None,
    title: str = "",
) -> dict:
    """Return a basic LXMF text message packet dict.

    Parameters
    ----------
    content:
        Message body text.
    source_hash:
        Sender's 16-byte hash as hex string.
    msg_id:
        32-byte SHA-256 message ID as hex string.
    timestamp:
        UNIX timestamp float.
    title:
        Optional message title.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id or ("cd" * 32),
        "timestamp": timestamp or 1700000000.0,
        "title": title,
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


# ---------------------------------------------------------------------------
# Title packets
# ---------------------------------------------------------------------------
# Derivation: LXMF-source-derived — text message with a non-empty title
# field, exercising the title-in-payload path.
# ---------------------------------------------------------------------------


def make_lxmf_title_packet(
    content: str = "body",
    title: str = "Subject Line",
    source_hash: str = "cd" * 16,
) -> dict:
    """Return an LXMF text message with a title.

    Parameters
    ----------
    content:
        Message body text.
    title:
        Message title / subject.
    source_hash:
        Sender's 16-byte hash as hex string.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": "aa" * 32,
        "timestamp": 1700000001.0,
        "title": title,
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


# ---------------------------------------------------------------------------
# Fields packets
# ---------------------------------------------------------------------------
# Derivation: synthetic scaffold — tests fields dict with a MEDRE envelope.
# ---------------------------------------------------------------------------


def make_lxmf_fields_packet(
    content: str = "fields test",
    fields: dict | None = None,
    source_hash: str = "ef" * 16,
) -> dict:
    """Return an LXMF text message with populated fields dict.

    Parameters
    ----------
    content:
        Message body text.
    fields:
        Fields dict (defaults to a MEDRE envelope placeholder).
    source_hash:
        Sender's 16-byte hash as hex string.
    """
    if fields is None:
        fields = {0xFD: {"medre": {"schema_version": 1, "event_id": "test-evt"}}}
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": "bb" * 32,
        "timestamp": 1700000002.0,
        "title": "",
        "content": content,
        "fields": fields,
        "signature_validated": True,
        "has_fields": True,
    }


# ---------------------------------------------------------------------------
# Attachment-only packets (unsupported)
# ---------------------------------------------------------------------------
# Derivation: synthetic scaffold — has fields with attachment keys but no
# content, exercising the "unsupported" category path.
# ---------------------------------------------------------------------------


def make_lxmf_attachment_packet(
    fields: dict | None = None,
) -> dict:
    """Return an LXMF packet with attachment fields but no content.

    Parameters
    ----------
    fields:
        Fields dict with attachment keys.  Defaults to a file attachment.
    """
    if fields is None:
        fields = {0x05: [{"name": "file.txt", "size": 100}]}
    return {
        "source_hash": "11" * 16,
        "destination_hash": "00" * 16,
        "message_id": "cc" * 32,
        "timestamp": 1700000003.0,
        "title": "",
        "content": None,
        "fields": fields,
        "signature_validated": False,
        "has_fields": True,
    }


# ---------------------------------------------------------------------------
# Minimal / empty packets
# ---------------------------------------------------------------------------
# Derivation: synthetic scaffold — bare-minimum packet for edge-case tests.
# ---------------------------------------------------------------------------


def make_lxmf_minimal_packet(
    content: str = "",
) -> dict:
    """Return a minimal LXMF packet, possibly with empty content.

    Parameters
    ----------
    content:
        Message body (empty string for minimal case).
    """
    return {
        "source_hash": "00" * 16,
        "destination_hash": "00" * 16,
        "message_id": "00" * 32,
        "timestamp": 0.0,
        "title": "",
        "content": content,
        "fields": None,
        "signature_validated": False,
        "has_fields": False,
    }


# ---------------------------------------------------------------------------
# Outbound result fixtures
# ---------------------------------------------------------------------------
# Derivation: synthetic scaffold — mock outbound delivery result.
# ---------------------------------------------------------------------------


def make_lxmf_outbound_result(
    content: str = "hello",
    message_id: str | None = None,
    dest_hash: str | None = None,
) -> dict:
    """Return a mock outbound send result dict.

    Parameters
    ----------
    content:
        Outbound message content text.
    message_id:
        Hex message ID.
    dest_hash:
        Destination hash hex string.
    """
    return {
        "content": content,
        "message_id": message_id or ("ff" * 32),
        "destination_hash": dest_hash or ("00" * 16),
    }


# ---------------------------------------------------------------------------
# Bytes-based content / title variants
# ---------------------------------------------------------------------------
# Derivation: LXMF-source-derived — exercises the bytes → str normalisation
# path for content and title fields.
# ---------------------------------------------------------------------------


def make_lxmf_bytes_content_packet(
    content: bytes = b"hello bytes",
    source_hash: str = "ab" * 16,
    msg_id: str | None = None,
) -> dict:
    """Return an LXMF text message with bytes content.

    Parameters
    ----------
    content:
        Message body as UTF-8 bytes.
    source_hash:
        Sender's 16-byte hash as hex string.
    msg_id:
        32-byte SHA-256 message ID as hex string.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id or ("cd" * 32),
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


def make_lxmf_bytes_title_packet(
    content: str = "body",
    title: bytes = b"Subject",
    source_hash: str = "cd" * 16,
) -> dict:
    """Return an LXMF text message with a bytes title.

    Parameters
    ----------
    content:
        Message body text.
    title:
        Message title as UTF-8 bytes.
    source_hash:
        Sender's 16-byte hash as hex string.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": "aa" * 32,
        "timestamp": 1700000001.0,
        "title": title,
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


def make_lxmf_invalid_utf8_packet(
    content: bytes = b"\xff\xfe\x00\x01",
) -> dict:
    """Return an LXMF packet with invalid UTF-8 content bytes.

    Parameters
    ----------
    content:
        Invalid UTF-8 bytes for the message body.
    """
    return {
        "source_hash": "ab" * 16,
        "destination_hash": "00" * 16,
        "message_id": "cd" * 32,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }
