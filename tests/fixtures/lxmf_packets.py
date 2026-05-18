"""Centralised LXMF packet fixture factories.

These factories produce plain-dict approximations of the message payload
structures carried by LXMF messages.  They are **MEDRE fixture
approximations**, not exhaustive captures — real LXMF messages may carry
additional fields not represented here.

Fixture provenance labels
-------------------------
Every fixture factory carries a **provenance label** in its docstring
indicating how closely it corresponds to real LXMF event field shapes:

* **source-derived** — shape verified against LXMF/Reticulum SDK event
  payloads.  Fields match real ``LXMessage`` attributes (``source_hash``,
  ``destination_hash``, ``content``, ``title``, ``fields``, ``timestamp``,
  ``has_fields``, ``delivery_method``, etc.) with high fidelity.

* **inferred** — shape inferred from LXMF documentation and Reticulum
  source code but not directly captured from real SDK event payloads.
  The basic structure is correct but specific field values may differ
  from real runtime output.

* **synthetic scaffold** — invented for MEDRE test coverage without direct
  correspondence to a specific real LXMF event capture.  The basic
  structure (``source_hash``, ``destination_hash``, ``message_id``,
  ``content``, ``fields``) is correct, but field values and sub-dict
  shapes are fabricated.

* **unknown** — placeholders whose correspondence to real LXMF event
  shapes has not been verified.  Treat with caution.

LXMF message semantics
----------------------
LXMF (Lightweight Extensible Message Format) messages carry:

* ``source_hash``: 16-byte sender identity hexhash.
* ``destination_hash``: 16-byte recipient identity hexhash.
* ``message_id``: 32-byte SHA-256 hash (also called "hash").
* ``timestamp``: UNIX float seconds.
* ``title``: UTF-8 string (optional).
* ``content``: UTF-8 string body.
* ``fields``: Extensible metadata dict.
* ``signature_validated``: Whether Ed25519 signature was verified.
* ``has_fields``: Whether fields dict is non-empty.
* ``delivery_method``: One of "direct", "opportunistic", "propagated",
  "paper".

Usage::

    from tests.fixtures.lxmf_packets import make_lxmf_text_packet

    pkt = make_lxmf_text_packet(content="hello", source_hash="ab"*16)
"""

# ---------------------------------------------------------------------------
# Text packets
# ---------------------------------------------------------------------------
# Provenance: source-derived — based on LXMF LXMessage event payload shape
# with content, source_hash, destination_hash, message_id, timestamp,
# delivery_method, and signature_validated fields matching real SDK output.
# ---------------------------------------------------------------------------


def make_lxmf_text_packet(
    content: str = "hello",
    source_hash: str = "ab" * 16,
    msg_id: str | None = None,
    timestamp: float | None = None,
    title: str = "",
    destination_hash: str = "00" * 16,
    delivery_method: str = "direct",
    signature_validated: bool = True,
) -> dict:
    """Return a basic LXMF text message packet dict.

    Provenance: source-derived.

    Parameters
    ----------
    content:
        Message body text.
    source_hash:
        Sender's 16-byte identity hexhash.
    msg_id:
        32-byte SHA-256 message ID as hex string.
    timestamp:
        UNIX timestamp float.
    title:
        Optional message title.
    destination_hash:
        Recipient's 16-byte identity hexhash.
    delivery_method:
        LXMF delivery method: direct, opportunistic, propagated, paper.
    signature_validated:
        Whether Ed25519 signature was verified.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": destination_hash,
        "message_id": msg_id or ("cd" * 32),
        "timestamp": timestamp or 1700000000.0,
        "title": title,
        "content": content,
        "fields": {},
        "signature_validated": signature_validated,
        "has_fields": False,
        "delivery_method": delivery_method,
    }


# ---------------------------------------------------------------------------
# Title packets
# ---------------------------------------------------------------------------
# Provenance: source-derived — text message with a non-empty title field,
# exercising the title-in-payload path.
# ---------------------------------------------------------------------------


def make_lxmf_title_packet(
    content: str = "body",
    title: str = "Subject Line",
    source_hash: str = "cd" * 16,
    delivery_method: str = "direct",
) -> dict:
    """Return an LXMF text message with a title.

    Provenance: source-derived.

    Parameters
    ----------
    content:
        Message body text.
    title:
        Message title / subject.
    source_hash:
        Sender's 16-byte identity hexhash.
    delivery_method:
        LXMF delivery method.
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
        "delivery_method": delivery_method,
    }


# ---------------------------------------------------------------------------
# Fields packets
# ---------------------------------------------------------------------------
# Provenance: synthetic scaffold — tests fields dict with a MEDRE envelope.
# ---------------------------------------------------------------------------


def make_lxmf_fields_packet(
    content: str = "fields test",
    fields: dict | None = None,
    source_hash: str = "ef" * 16,
    delivery_method: str = "direct",
) -> dict:
    """Return an LXMF text message with populated fields dict.

    Provenance: synthetic scaffold.

    Parameters
    ----------
    content:
        Message body text.
    fields:
        Fields dict (defaults to a MEDRE envelope placeholder).
    source_hash:
        Sender's 16-byte identity hexhash.
    delivery_method:
        LXMF delivery method.
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
        "delivery_method": delivery_method,
    }


# ---------------------------------------------------------------------------
# Delivery method variants
# ---------------------------------------------------------------------------
# Provenance: inferred — based on LXMF delivery method documentation.
# ---------------------------------------------------------------------------


def make_lxmf_propagated_packet(
    content: str = "propagated message",
    source_hash: str = "ab" * 16,
) -> dict:
    """Return an LXMF text message with propagated delivery method.

    Provenance: inferred.

    Parameters
    ----------
    content:
        Message body text.
    source_hash:
        Sender's 16-byte identity hexhash.
    """
    return make_lxmf_text_packet(
        content=content,
        source_hash=source_hash,
        delivery_method="propagated",
    )


def make_lxmf_opportunistic_packet(
    content: str = "opportunistic message",
    source_hash: str = "ab" * 16,
) -> dict:
    """Return an LXMF text message with opportunistic delivery method.

    Provenance: inferred.

    Parameters
    ----------
    content:
        Message body text.
    source_hash:
        Sender's 16-byte identity hexhash.
    """
    return make_lxmf_text_packet(
        content=content,
        source_hash=source_hash,
        delivery_method="opportunistic",
    )


def make_lxmf_paper_packet(
    content: str = "paper message",
    source_hash: str = "ab" * 16,
) -> dict:
    """Return an LXMF text message with paper delivery method.

    Provenance: inferred.

    Parameters
    ----------
    content:
        Message body text.
    source_hash:
        Sender's 16-byte identity hexhash.
    """
    return make_lxmf_text_packet(
        content=content,
        source_hash=source_hash,
        delivery_method="paper",
    )


# ---------------------------------------------------------------------------
# Attachment-only packets (unsupported)
# ---------------------------------------------------------------------------
# Provenance: synthetic scaffold — has fields with attachment keys but no
# content, exercising the "unsupported" category path.
# ---------------------------------------------------------------------------


def make_lxmf_attachment_packet(
    fields: dict | None = None,
) -> dict:
    """Return an LXMF packet with attachment fields but no content.

    Provenance: synthetic scaffold.

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
        "delivery_method": "direct",
    }


# ---------------------------------------------------------------------------
# Minimal / empty packets
# ---------------------------------------------------------------------------
# Provenance: synthetic scaffold — bare-minimum packet for edge-case tests.
# ---------------------------------------------------------------------------


def make_lxmf_minimal_packet(
    content: str = "",
) -> dict:
    """Return a minimal LXMF packet, possibly with empty content.

    Provenance: synthetic scaffold.

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
        "delivery_method": None,
    }


# ---------------------------------------------------------------------------
# Malformed packets
# ---------------------------------------------------------------------------
# Provenance: synthetic scaffold — malformed packets for boundary testing.
# ---------------------------------------------------------------------------


def make_lxmf_malformed_missing_source(
    content: str = "no source",
) -> dict:
    """Return an LXMF packet missing the source_hash field.

    Provenance: synthetic scaffold.

    Parameters
    ----------
    content:
        Message body text.
    """
    return {
        "destination_hash": "00" * 16,
        "message_id": "cd" * 32,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": False,
        "has_fields": False,
    }


def make_lxmf_malformed_wrong_types(
    content: int = 12345,
    source_hash: int = 999,
) -> dict:
    """Return an LXMF packet with wrong field types.

    Provenance: synthetic scaffold.

    Parameters
    ----------
    content:
        Content field as int instead of str/bytes.
    source_hash:
        Source hash as int instead of str/bytes.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": "cd" * 32,
        "timestamp": "not_a_number",
        "title": "",
        "content": content,
        "fields": "not_a_dict",
        "signature_validated": "yes",
        "has_fields": "no",
    }


# ---------------------------------------------------------------------------
# Outbound result fixtures
# ---------------------------------------------------------------------------
# Provenance: synthetic scaffold — mock outbound delivery result.
# ---------------------------------------------------------------------------


def make_lxmf_outbound_result(
    content: str = "hello",
    message_id: str | None = None,
    dest_hash: str | None = None,
) -> dict:
    """Return a mock outbound send result dict.

    Provenance: synthetic scaffold.

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
# Provenance: source-derived — exercises the bytes → str normalisation
# path for content and title fields observed in real LXMF wire format.
# ---------------------------------------------------------------------------


def make_lxmf_bytes_content_packet(
    content: bytes = b"hello bytes",
    source_hash: str = "ab" * 16,
    msg_id: str | None = None,
) -> dict:
    """Return an LXMF text message with bytes content.

    Provenance: source-derived.

    Parameters
    ----------
    content:
        Message body as UTF-8 bytes.
    source_hash:
        Sender's 16-byte identity hexhash.
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
        "delivery_method": "direct",
    }


def make_lxmf_bytes_title_packet(
    content: str = "body",
    title: bytes = b"Subject",
    source_hash: str = "cd" * 16,
) -> dict:
    """Return an LXMF text message with a bytes title.

    Provenance: source-derived.

    Parameters
    ----------
    content:
        Message body text.
    title:
        Message title as UTF-8 bytes.
    source_hash:
        Sender's 16-byte identity hexhash.
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
        "delivery_method": "direct",
    }


def make_lxmf_invalid_utf8_packet(
    content: bytes = b"\xff\xfe\x00\x01",
) -> dict:
    """Return an LXMF packet with invalid UTF-8 content bytes.

    Provenance: synthetic scaffold.

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
        "delivery_method": "direct",
    }


# ---------------------------------------------------------------------------
# Bytes source_hash / message_id variants
# ---------------------------------------------------------------------------
# Provenance: source-derived — real LXMF payloads may carry raw bytes
# for source_hash and message_id instead of hex strings.
# ---------------------------------------------------------------------------


def make_lxmf_bytes_source_hash_packet(
    content: str = "hello",
    source_hash: bytes = b"\xab" * 16,
    msg_id: bytes = b"\xcd" * 32,
) -> dict:
    """Return an LXMF text message with bytes source_hash and message_id.

    Provenance: source-derived.

    Parameters
    ----------
    content:
        Message body text.
    source_hash:
        Sender's 16-byte identity hash as raw bytes.
    msg_id:
        32-byte SHA-256 message ID as raw bytes.
    """
    return {
        "source_hash": source_hash,
        "destination_hash": b"\x00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
        "delivery_method": "direct",
    }
