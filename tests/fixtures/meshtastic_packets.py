"""Centralised Meshtastic packet fixture factories.

These factories produce plain-dict approximations of the packet structures
emitted by meshtastic-python (mtjk fork) TCP/BLE/serial callbacks.  They are
**MEDRE fixture approximations**, not exhaustive hardware captures -- real
packets may carry additional protobuf fields not represented here.

Fixture provenance labels
-------------------------
Every fixture factory carries a **provenance label** in its docstring
indicating how closely it corresponds to real Meshtastic packet shapes:

* **mtjk-derived** — shape verified against the installed ``mtjk`` package's
  source code (``_normalize_packet_from_radio``, ``_enrich_packet_identity``,
  ``_on_text_receive``, protobuf ``MessageToDict`` output).  These fixtures
  match real callback packet dicts with high fidelity.

* **MMRelay-derived** — shape observed in the old ``mmrelay`` codebase
  (``/home/jeremiah/dev/meshtastic-matrix-relay``).  These fixtures carry
  additional fields (``rxTime``, ``rxRssi``, ``rxSnr``, ``to``) that real
  callbacks include, verified through MMRelay's ``on_meshtastic_message``
  handler.

* **synthetic scaffold** — invented for MEDRE test coverage without direct
  correspondence to a specific real packet capture.  The basic structure
  (``from``, ``fromId``, ``toId``, ``channel``, ``id``, ``decoded``) is
  correct, but field values and sub-dict shapes are fabricated.

* **unverified** — placeholders whose correspondence to real packet shapes
  has not been verified against either mtjk source or MMRelay observations.
  Treat with caution; may diverge from real hardware output.

Usage::

    from tests.fixtures.meshtastic_packets import make_text_packet

    pkt = make_text_packet(text="hello mesh", sender="!deadbeef")

Portnum convention: these fixtures include both MEDRE-normalised lowercase
portnum strings (e.g. "text_message", "telemetry", "admin") and the real
symbolic meshtastic-python / mtjk strings used by callback dictionaries
(e.g. "TEXT_MESSAGE_APP", "TELEMETRY_APP", "ADMIN_APP").  The MEDRE
``_NUMERIC_PORTNUM_MAP`` is **fixture scaffold only** — see
``docs/contracts/10-meshtastic-source-audit.md`` for the authoritative
protobuf PortNum table.
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
# Provenance: mtjk-derived.
# The ``from`` numeric field mirrors the real mtjk callback payload where
# both ``from`` (NodeNum int) and ``fromId`` (hex string) are present.
# ``toId`` is populated by mtjk's ``_enrich_packet_identity()``.
# ``decoded.text`` is populated by mtjk's ``_on_text_receive()``.
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

    Provenance: mtjk-derived.
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
    """Text packet that references a previous message via ``replyId``.

    Provenance: mtjk-derived (``decoded.replyId`` verified in protobuf
    ``Data.reply_id``).
    """
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
    """Text packet addressed to a specific node (non-empty ``toId``).

    Provenance: mtjk-derived (``toId`` populated by
    ``_enrich_packet_identity``; DM detection in MMRelay compares numeric
    ``to`` against ``myInfo.my_node_num``).
    """
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
    """Text packet with empty ``toId`` (channel broadcast).

    Provenance: mtjk-derived (empty ``toId`` indicates broadcast;
    ``to == 0xFFFFFFFF`` is ``NODENUM_BROADCAST``).
    """
    return make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        to_id="",
    )


# ---------------------------------------------------------------------------
# Non-text portnum variants
# Provenance: synthetic scaffold unless otherwise noted.
# The ``decoded.portnum`` values use MEDRE-normalised names.  See
# ``make_symbolic_*`` variants for real mtjk callback portnum strings.
# ---------------------------------------------------------------------------


def make_telemetry_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Telemetry packet (device metrics, environment metrics, etc.).

    Provenance: synthetic scaffold.  The portnum value ``"telemetry"`` is
    MEDRE-normalised; real callbacks carry ``"TELEMETRY_APP"``.  The
    ``decoded`` sub-dict shape is not verified against real hardware output.
    """
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
    """Position / GPS packet.

    Provenance: synthetic scaffold.  The portnum value ``"position"`` is
    MEDRE-normalised; real callbacks carry ``"POSITION_APP"``.  The
    ``decoded`` sub-dict does not include real GPS fields (latitude,
    longitude, altitude, etc.).
    """
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
    """Node-info advertisement packet.

    Provenance: synthetic scaffold.  The portnum value ``"nodeinfo"`` is
    MEDRE-normalised; real callbacks carry ``"NODEINFO_APP"``.  The
    ``decoded`` sub-dict does not include real user info fields (long name,
    short name, hw model, etc.).
    """
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
    """Admin portnum packet using MEDRE-normalised ``admin``.

    Provenance: synthetic scaffold.  ``"admin"`` is MEDRE-normalised;
    real callbacks carry ``"ADMIN_APP"``.  No admin payload fields
    are included.
    """
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

    Provenance: synthetic scaffold.  The ``"text_message_ack"`` portnum
    does **not** correspond to any real protobuf enum value.  Real ACKs
    arrive via ``"ROUTING_APP"`` with ``decoded.routing.errorReason ==
    "NONE"``.  See ``make_symbolic_routing_ack_packet`` for the real ACK
    shape.  Full ACK handling is deferred to a later tranche.
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
    """Text packet using real symbolic ``TEXT_MESSAGE_APP`` portnum.

    Provenance: mtjk-derived.  ``TEXT_MESSAGE_APP`` is the real protobuf
    enum name returned by ``PortNum.Name()`` in mtjk callbacks.
    """
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
    """Telemetry packet using real symbolic ``TELEMETRY_APP`` portnum.

    Provenance: mtjk-derived (portnum name verified).  ``decoded`` payload
    shape is synthetic scaffold — real telemetry includes device_metrics
    and environment_metrics sub-dicts not represented here.
    """
    pkt = make_telemetry_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "TELEMETRY_APP"
    return pkt


def make_symbolic_position_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Position packet using real symbolic ``POSITION_APP`` portnum.

    Provenance: mtjk-derived (portnum name verified).  ``decoded`` payload
    shape is synthetic scaffold — real position includes latitudeI,
    longitudeI, altitude, etc.
    """
    pkt = make_position_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "POSITION_APP"
    return pkt


def make_symbolic_nodeinfo_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Node-info packet using real symbolic ``NODEINFO_APP`` portnum.

    Provenance: mtjk-derived (portnum name verified).  ``decoded`` payload
    shape is synthetic scaffold — real nodeinfo includes user sub-dict
    with longName, shortName, hwModel, etc.
    """
    pkt = make_nodeinfo_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "NODEINFO_APP"
    return pkt


def make_symbolic_admin_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Admin packet using real symbolic ``ADMIN_APP`` portnum.

    Provenance: mtjk-derived (portnum name verified).
    """
    pkt = make_admin_packet(sender=sender, packet_id=packet_id, channel=channel)
    pkt["decoded"]["portnum"] = "ADMIN_APP"
    return pkt


def make_symbolic_routing_ack_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Routing ACK-like packet using real symbolic ``ROUTING_APP`` portnum.

    Provenance: mtjk-derived.  Real ACKs arrive via ``ROUTING_APP`` with
    ``decoded.routing.errorReason == "NONE"``.  MMRelay uses this shape
    for health probe ACK responses.  The routing sub-dict is verified
    against the protobuf ``Routing`` message.
    """
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
    """Packet with an unsupported symbolic ``*_APP`` portnum.

    Provenance: synthetic scaffold.
    """
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
    """Generic plugin / custom-portnum packet.

    Provenance: synthetic scaffold.
    """
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
# Provenance: synthetic scaffold unless otherwise noted.
# ---------------------------------------------------------------------------


def make_minimal_packet(
    from_id: str = "!node1",
    packet_id: int = 1,
) -> dict:
    """Bare-minimum packet — no ``decoded``, no ``channel``.

    Useful for testing graceful degradation when a packet is missing
    expected keys.

    Provenance: synthetic scaffold.
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
    """Packet whose ``portnum`` is not recognised by the codec.

    Provenance: synthetic scaffold.
    """
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

    Provenance: unverified.  The numeric-to-string portnum mapping in
    MEDRE's classifier is scaffold-only and does not match the real
    protobuf enum for most values.  See
    ``docs/contracts/10-meshtastic-source-audit.md`` Section 3.2.
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
# Provenance: mtjk-derived (numeric NodeNum fields verified in
# ``_enrich_packet_identity`` and ``MessageToDict`` output).
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

    Provenance: mtjk-derived.
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

    Provenance: mtjk-derived.
    """
    return {
        "from": from_numeric,
        "fromId": from_id,
        "toId": "",
        "channel": 0,
        "id": packet_id,
        "decoded": {"portnum": "text_message", "text": "numeric from test"},
    }


# ---------------------------------------------------------------------------
# MMRelay-derived fixtures
# ---------------------------------------------------------------------------
# Provenance: MMRelay-derived.
# These factory functions produce packet shapes observed in the old MMRelay
# codebase (``/home/jeremiah/dev/meshtastic-matrix-relay``).  They include
# fields that real mtjk callbacks produce but MEDRE's basic fixtures may
# omit.  Not all MMRelay-observed fields are required for MEDRE's tests --
# extras like ``rxTime``, ``rxRssi``, ``rxSnr`` are included for realism
# where useful.
# ---------------------------------------------------------------------------


def make_mmrelay_style_text_packet(
    text: str = "hello from mmrelay",
    sender: str = "!abc123",
    channel: int = 0,
    packet_id: int = 12345,
    to_id: str | None = None,
    rx_time: int = 0,
    rx_rssi: int = -80,
    rx_snr: float = 7.5,
) -> dict:
    """Text packet resembling old MMRelay ``on_meshtastic_message`` shape.

    Adds realistic ``rxTime``, ``rxRssi``, and ``rxSnr`` fields that the
    mtjk callback includes alongside the decoded payload.

    Provenance: MMRelay-derived.
    """
    pkt = make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        portnum="TEXT_MESSAGE_APP",
    )
    pkt["rxTime"] = rx_time
    pkt["rxRssi"] = rx_rssi
    pkt["rxSnr"] = rx_snr
    pkt["to"] = 0xFFFFFFFF  # BROADCAST_NUM — always present in real packets
    if to_id is not None:
        pkt["toId"] = to_id
    return pkt


def make_emoji_reaction_packet(
    text: str = "\U0001f44d",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 100,
    reply_to_id: int = 50,
) -> dict:
    """Reaction / emoji packet as observed in old MMRelay.

    MMRelay detected reactions via ``decoded.emoji == 1`` together with
    ``decoded.replyId``.  The ``emoji`` field is an int flag, not a
    character.  The reaction symbol is carried in ``decoded.text``.

    Provenance: MMRelay-derived.
    """
    pkt = make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        portnum="TEXT_MESSAGE_APP",
    )
    pkt["decoded"]["emoji"] = 1
    pkt["decoded"]["replyId"] = reply_to_id
    return pkt


def make_encrypted_packet(
    sender: str = "!node1",
    packet_id: int = 10,
    channel: int = 0,
) -> dict:
    """Encrypted packet as received by MMRelay.

    Real encrypted packets carry ``encrypted: true`` and may lack a
    decoded payload.  MMRelay classified encrypted packets separately
    via ``packet.get("encrypted")`` with configurable action.

    Provenance: MMRelay-derived.
    """
    return {
        "from": _node_num(sender),
        "fromId": sender,
        "toId": "",
        "to": 0xFFFFFFFF,
        "channel": channel,
        "id": packet_id,
        "encrypted": True,
        "decoded": {"portnum": "TEXT_MESSAGE_APP"},
    }


def make_rxtime_packet(
    text: str = "packet with rx timestamp",
    sender: str = "!node1",
    packet_id: int = 42,
    rx_time: int = 1700000000,
) -> dict:
    """Text packet with ``rxTime`` field for backlog suppression tests.

    Real mtjk callbacks include ``rxTime`` (Unix seconds), which MMRelay
    used for startup backlog suppression.  This fixture allows MEDRE to
    test rxTime-aware filtering when that feature is implemented.

    Provenance: MMRelay-derived.  The ``rxTime`` field is verified in the
    protobuf ``MeshPacket.rx_time`` and used by MMRelay's backlog
    suppression logic (``STARTUP_PACKET_DRAIN_SECS`` and
    ``RELAY_START_TIME`` comparison).
    """
    pkt = make_text_packet(
        text=text,
        sender=sender,
        packet_id=packet_id,
        portnum="TEXT_MESSAGE_APP",
    )
    pkt["rxTime"] = rx_time
    pkt["to"] = 0xFFFFFFFF
    return pkt


# ---------------------------------------------------------------------------
# Startup / backlog suppression variants
# ---------------------------------------------------------------------------
# Provenance: MMRelay-derived.
# These fixtures model the startup backlog scenario where a Meshtastic node
# replays recently-received packets on TCP connect.  MMRelay suppresses
# packets whose ``rxTime`` predates the connection start time (adjusted for
# clock skew).  MEDRE's ``startup_backlog_suppress_seconds`` config field
# exists but is not yet implemented.
# ---------------------------------------------------------------------------


def make_stale_backlog_packet(
    text: str = "stale message from before connect",
    sender: str = "!node1",
    packet_id: int = 42,
    rx_time: int = 1700000000,
    rx_rssi: int = -90,
    rx_snr: float = 5.25,
    channel: int = 0,
) -> dict:
    """Text packet resembling stale backlog received on initial connect.

    When a Meshtastic node is connected via TCP, it may replay recently-
    buffered packets whose ``rxTime`` predates the client's connection
    time.  This fixture models that scenario with an old ``rxTime`` value.

    MEDRE does not yet implement backlog suppression; this fixture is for
    future testing of ``startup_backlog_suppress_seconds``.

    Provenance: MMRelay-derived.  Shape matches packets observed during
    MMRelay startup drain period; ``rxTime`` is set to a deliberately old
    Unix timestamp to simulate stale traffic.
    """
    pkt = make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        portnum="TEXT_MESSAGE_APP",
    )
    pkt["rxTime"] = rx_time
    pkt["rxRssi"] = rx_rssi
    pkt["rxSnr"] = rx_snr
    pkt["to"] = 0xFFFFFFFF
    return pkt


def make_channel_message_packet(
    text: str = "message on channel 3",
    sender: str = "!abc123",
    channel: int = 3,
    packet_id: int = 77,
) -> dict:
    """Text packet on a specific non-default channel index.

    This is a convenience wrapper for testing channel-aware routing.
    Real Meshtastic traffic uses channel indices 0-7.

    Provenance: mtjk-derived (channel field is protobuf
    ``MeshPacket.channel``, verified as int in callbacks).
    """
    return make_text_packet(
        text=text,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
        to_id="",
    )
