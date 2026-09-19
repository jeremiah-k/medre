"""MEDRE-derived MeshCore message identity.

The declared pinned MeshCore SDK exposes **no native message identifier** on
received text payloads. ``CONTACT_MSG_RECV`` / ``CHANNEL_MSG_RECV`` payloads
carry only:

* ``type`` — ``"PRIV"`` (direct) or ``"CHAN"`` (channel)
* ``pubkey_prefix`` — 6-byte hex sender key prefix (direct messages)
* ``channel_idx`` — channel index (channel messages)
* ``txt_type`` — message sub-type code
* ``sender_timestamp`` — sender-assigned ``uint32`` Unix timestamp,
  one-second resolution
* ``text`` — decoded payload text

The firmware itself treats the timestamp as one input among several that
make the radio packet hash unique (``BaseChatMesh.cpp`` copies it into the
payload as "mostly an extra blob to help make packet_hash unique"); it is
not an identifier on its own.  Two distinct messages typed within the same
second by the same sender therefore share ``sender_timestamp``.

MEDRE derives a deterministic dedup identity from the identity-bearing
fields — direct/ channel scope, sender, channel index, timestamp,
sub-type, and text: the same field set the protocol relies on to
distinguish packets on the wire.  Reception-volatile fields (``RSSI``,
``SNR``, ``recv_time``, ``attempt``, ``path``) are deliberately excluded
so a genuine retransmission of one message keeps one identity across
repeated callbacks and process restarts.

The identity is a full SHA-256 hexdigest (never truncated) prefixed with
``mc1-``.  It is a MEDRE-derived identity, not an SDK-provided one; the
native ``sender_timestamp`` remains recorded under
``metadata.native.data["meshcore"]["packet_id"]``.

Residual ambiguity (protocol-inherent, not fixable at this layer): two
genuinely distinct messages with identical sender, channel, timestamp,
sub-type, and text are indistinguishable on the wire.  MEDRE treats the
second as a retransmission of the first; exactly-once separation of such
inputs cannot be claimed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "MESHCORE_IDENTITY_PREFIX",
    "derive_message_identity",
]

# Domain-separation string for the derived identity.  Never parsed for
# meaning; bump the version only if the identity field set changes.
_IDENTITY_DOMAIN = "medre-meshcore-message-identity-v1"

MESHCORE_IDENTITY_PREFIX = "mc1-"
"""Prefix marking a MEDRE-derived MeshCore message identity."""


def derive_message_identity(
    *,
    sender_id: str | None,
    channel_index: int | None,
    sender_timestamp: int | None,
    txt_type: Any,
    text: str | None,
    is_direct_message: bool,
) -> str | None:
    """Derive the deterministic MeshCore message identity digest.

    Parameters
    ----------
    sender_id:
        Sender ``pubkey_prefix``, or ``None`` / ``""`` when the payload
        carries no sender scope (channel broadcasts).  Both absent forms
        normalise to the same null scope component.
    channel_index:
        Effective channel index, or ``None`` for direct messages.
    sender_timestamp:
        Sender-assigned ``uint32`` Unix timestamp.  When ``None`` there
        is no stable identity anchor and the function returns ``None``;
        callers must then claim no native identity (no dedup key, no
        native ref) rather than inventing one.
    txt_type:
        Message sub-type code (SDK-conformant input is ``int``).
    text:
        Decoded payload text; ``None`` normalises to ``""``.
    is_direct_message:
        Whether the payload classified as a direct (``PRIV``) message.

    Returns
    -------
    str | None
        ``"<prefix><64-char lowercase hex>"`` derived from the
        identity-bearing fields, or ``None`` when *sender_timestamp* is
        missing.
    """
    if sender_timestamp is None:
        return None

    identity = json.dumps(
        {
            "dm": bool(is_direct_message),
            "sender": sender_id or None,
            "channel": channel_index,
            "ts": sender_timestamp,
            "txt_type": _scalar(txt_type),
            "text": "" if text is None else text,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256((_IDENTITY_DOMAIN + identity).encode("utf-8")).hexdigest()
    return MESHCORE_IDENTITY_PREFIX + digest


def _scalar(value: Any) -> Any:
    """Coerce an optional native field to a JSON-stable scalar.

    SDK-conformant values (``int`` / ``str`` / ``None``) pass through
    unchanged.  ``bytes`` is hex-encoded; any other exotic type falls
    back to ``str()`` so identity derivation can never crash ingress.
    Determinism is guaranteed for SDK-conformant packets only.
    """
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
