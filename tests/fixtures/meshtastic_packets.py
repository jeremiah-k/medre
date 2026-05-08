"""Centralised Meshtastic packet fixture factories.

These factories produce plain-dict approximations of the packet structures
emitted by meshtastic-python TCP/BLE/serial callbacks.  They are **MEDRE
fixture approximations**, not exhaustive hardware captures — real packets may
carry additional protobuf fields not represented here.

Usage::

    from tests.fixtures.meshtastic_packets import make_text_packet

    pkt = make_text_packet(text="hello mesh", sender="!deadbeef")

Portnum convention: these fixtures include both MEDRE-normalised lowercase
portnum strings (e.g. "text_message", "telemetry", "admin") and the real
symbolic meshtastic-python / mtjk strings used by callback dictionaries
(e.g. "TEXT_MESSAGE_APP", "TELEMETRY_APP", "ADMIN_APP").  Full protobuf
enum coverage is deferred -- these fixtures test the MEDRE runtime
boundary, not exhaustive hardware protocol fidelity.
"""


def _node_num(node_id: str) -> int:
    """Convert a hex node-id string (``!abc123``) to a numeric NodeNum.

    Non-hex suffixes (e.g. ``!node1``) are treated as 0 — these are test
    fixtures, not real hardware identifiers.
    """
    if node_id.startswith("!"):
        try:
            return int(node_id[1:], 16)
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Text-message variants
# ---------------------------------------------------------------------------

def make_text_packet(
    text: str = "hello mesh",
    sender: str = "!abc123",
    channel: int = 0,
    packet_id: int = 42,
    to_id: str = "",
    want_ack: bool = False,
    portnum: str = "text_message",
) -> dict:
    """Return a canonical text-message packet dict.

    This is the workhorse fixture — every other text-based helper delegates
    here.  The ``from`` numeric field mirrors the real meshtastic-python
    callback payload where both ``from`` (NodeNum int) and ``fromId`` (hex
    string) are present.
    """
    decoded: dict = {"portnum": portnum, "text": text}
    if want_ack:
        decoded["wantAck"] = True

    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": to_id,
        "channel": channel,
        "id": packet_id,
        "decoded": decoded,
    }


def make_text_packet_with_reply(
    text: str = "reply message",
    sender: str = "!abc123",
    channel: int = 0,
    packet_id: int = 100,
    reply_to_id: int = 50,
    to_id: str = "",
) -> dict:
    """Text packet that references a previous message via ``replyId``."""
    pkt = make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        to_id=to_id,
    )
    pkt["decoded"]["replyId"] = reply_to_id
    return pkt


def make_direct_message_packet(
    text: str = "private message",
    sender: str = "!abc123",
    target: str = "!target99",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Text packet addressed to a specific node (non-empty ``toId``)."""
    return make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        to_id=target,
    )


def make_broadcast_packet(
    text: str = "broadcast",
    sender: str = "!abc123",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Text packet with empty ``toId`` (channel broadcast)."""
    return make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        to_id="",
    )


# ---------------------------------------------------------------------------
# Non-text portnum variants
# ---------------------------------------------------------------------------

def make_telemetry_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Telemetry packet (device metrics, environment metrics, etc.)."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": "telemetry"},
    }


def make_position_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Position / GPS packet."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": "position"},
    }


def make_nodeinfo_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Node-info advertisement packet."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": "nodeinfo"},
    }


def make_admin_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Admin portnum packet using MEDRE-normalised ``admin``."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": "admin"},
    }


def make_ack_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """ACK packet with ``text_message_ack`` portnum.

    Note: the classifier matches ``"text_message_ack"`` (simplified
    MEDRE portnum string).  Real meshtastic-python ACKs arrive via
    ``"ROUTING_APP"`` with ``decoded.routing.errorReason == "NONE"``.
    Full ACK handling is deferred to a later tranche.
    """
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": "text_message_ack"},
    }


def make_symbolic_text_packet(
    text: str = "hello symbolic mesh",
    sender: str = "!abc123",
    channel: int = 0,
    packet_id: int = 42,
    to_id: str = "",
) -> dict:
    """Text packet using real symbolic ``TEXT_MESSAGE_APP`` portnum."""
    return make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        to_id=to_id,
        portnum="TEXT_MESSAGE_APP",
    )


def make_symbolic_telemetry_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Telemetry packet using real symbolic ``TELEMETRY_APP`` portnum."""
    pkt = make_telemetry_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "TELEMETRY_APP"
    return pkt


def make_symbolic_position_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Position packet using real symbolic ``POSITION_APP`` portnum."""
    pkt = make_position_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "POSITION_APP"
    return pkt


def make_symbolic_nodeinfo_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Node-info packet using real symbolic ``NODEINFO_APP`` portnum."""
    pkt = make_nodeinfo_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "NODEINFO_APP"
    return pkt


def make_symbolic_admin_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Admin packet using real symbolic ``ADMIN_APP`` portnum."""
    pkt = make_admin_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "ADMIN_APP"
    return pkt


def make_symbolic_routing_ack_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Routing ACK-like packet using real symbolic ``ROUTING_APP`` portnum."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {
            "portnum": "ROUTING_APP",
            "routing": {"errorReason": "NONE"},
        },
    }


def make_unknown_symbolic_app_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    portnum: str = "UNKNOWN_FUTURE_APP",
) -> dict:
    """Packet with an unsupported symbolic ``*_APP`` portnum."""
    return make_unknown_portnum_packet(
        sender=sender,
        packet_id=packet_id,
        portnum=portnum,
    )


def make_plugin_packet(
    portnum: str = "plugin_custom",
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Generic plugin / custom-portnum packet."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": portnum},
    }


# ---------------------------------------------------------------------------
# Edge-case / special variants
# ---------------------------------------------------------------------------

def make_minimal_packet(
    from_id: str = "!node1",
    packet_id: int = 1,
) -> dict:
    """Bare-minimum packet — no ``decoded``, no ``channel``.

    Useful for testing graceful degradation when a packet is missing
    expected keys.
    """
    return {
        "from": _node_num(from_id),
        "fromId": from_id,
        "id": packet_id,
    }


def make_unknown_portnum_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    portnum: str = "some_unknown_type",
) -> dict:
    """Packet whose ``portnum`` is not recognised by the codec."""
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": 0,
        "id": packet_id,
        "decoded": {"portnum": portnum},
    }


def make_numeric_portnum_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    portnum: int = 9,  # e.g. AUDIO_APP
) -> dict:
    """Packet whose ``portnum`` is a raw integer instead of a string.

    Some firmware builds and older protobuf versions emit the numeric enum
    value rather than the symbolic name.
    """
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "channel": 0,
        "id": packet_id,
        "decoded": {"portnum": portnum},
    }


# ---------------------------------------------------------------------------
# Numeric ``to`` / ``from`` helpers
# ---------------------------------------------------------------------------

def make_packet_with_numeric_to(
    sender: str = "!node1",
    to: int = 0xFFFFFFFF,  # NODENUM_BROADCAST
    to_id: str = "4294967295",
    packet_id: int = 42,
) -> dict:
    """Packet that carries both ``to`` (int) and ``toId`` (str).

    Meshtastic-python callbacks include the numeric NodeNum alongside the
    string representation.  ``0xFFFFFFFF`` is the real ``NODENUM_BROADCAST``
    constant.
    """
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "to": to,
        "toId": to_id,
        "channel": 0,
        "id": packet_id,
        "decoded": {"portnum": "text_message", "text": "numeric to test"},
    }


def make_packet_with_numeric_from(
    from_id: str = "!abc123",
    from_numeric: int = 123,
    packet_id: int = 42,
) -> dict:
    """Packet with both ``from`` (int NodeNum) and ``fromId`` (str).

    Allows tests to verify handling when the numeric ``from`` does **not**
    match the hex-derived value of ``fromId``.
    """
    return {
        "from": from_numeric,
        "fromId": from_id,
        "toId": "",
        "channel": 0,
        "id": packet_id,
        "decoded": {"portnum": "text_message", "text": "numeric from test"},
    }
