# 201: MeshCore channel relays render the firmware wire sender name

MeshCore's channel protocol (`CHANNEL_MSG_RECV`) carries no sender
identity — only direct messages have a `pubkey_prefix`. The firmware
therefore prepends the sending node's advertised name to every group
text on the wire (`"<name>: <text>"`), which the codec passed through
verbatim while leaving the attribution label unset. Relay prefixes
built on `{sender}` rendered an empty field for every MeshCore channel
source, e.g. in a Matrix room with `relay_prefix: "{sender}/{origin_label}: "`
(observed live):

    Meshtastic d662/medre-lab: MX-Xmt2MX-abab88e5   <- correct
    /medre-lab: MEDRE-MC-B: MX-Xmc2MX-fe7dd0bb      <- empty {sender}

`MeshCoreCodec.decode()` now lifts the wire-embedded name into the
native `contact_label` for channel texts when no known-contact label was
resolved, so `{sender}` renders the sender node name
(`MEDRE-MC-B/medre-lab: ...`). The message body stays verbatim (wire
fidelity is pinned by the live pair suite), DMs are excluded (they carry
real identity), and an explicitly resolved known-contact label still
wins. The defended contract that an opaque pubkey prefix never becomes
the sender label is unchanged.

The same release adds a device-free six-edge mesh interop regression
suite (`tests/test_mesh_interop_pipeline.py`) covering MT<->MC<->LX
radio-to-radio relay through the real codecs, renderers, and router —
previously untested — including the wire-name attribution path.
