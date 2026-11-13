# Correct the frozen adapter-config schema defaults and refresh release doc truth

The frozen `adapter-config.schema.json` published two defaults that contradicted
the installed adapters. The Meshtastic arm published
`message_delay_seconds: 0.5` where the pinned adapter dataclass, its README, and
core's operations guide default to `2.2` (margin above current firmware's
two-second phone-originated text limit), and the MeshCore arm published
`max_text_bytes: 512` where the dataclass, `GROUP_TEXT_BYTES`, and the adapter
README default to `160`. The schema now states both true defaults; the
Meshtastic pacing description explains the firmware-limit rationale instead of
generic copy, and the MeshCore budget description distinguishes the 160-byte
operator default from the prefix-aware 127-byte planning budget that the
capability declaration reports before the live device-name handshake. Core's
`docs/ops/configuration.md` MeshCore table row repeated the wrong 512 and is
corrected to `160`.

The near-description-less XMPP, Chatmail, and SimpleXChat arms now carry
accurate per-field descriptions drawn from each provider dataclass, including
the real `relay_prefix` contract: placeholders resolve from per-message relay
attribution, `""` disables prefixing, no `{nameN}` truncation variables are
declared on these transports, and unknown placeholders or formatting errors
fail the delivery instead of emitting a malformed prefix.

The `v0.1.0.freeze.json` adapter-config digest moves for the third time; every
other frozen schema entry is byte-for-byte unchanged, and this intentional
digest move carries the schema corrections above. Release signoff is required
before accepting the moved digest.

Docs truth fixes ride in the same change: `docs/spec/transport-profiles/xmpp.md`
now declares `metadata_fields: True` (matching the pinned adapter and its
capability JSON), and a new SDK-free binding test parses every transport-profile
capability table and fails when any stated value contradicts the sibling
`<transport>-capabilities.json` entry, so md-table value drift dies in CI
without SDKs. The G13 limitations indexes are re-verified at the current seven
manifest pins: the provenance table cites the manifest commits, the retired
MMRelay interop entry describes the removed surface and today's untrusted-input
tolerance boundary instead of the deleted opt-in output path, and the
encrypted-transport live-tier `pytest.xfail` guards are catalogued with
one-line reasons. The contract-gap register rewrites G14's current state
(retired `medre.interop` module and compatibility switches, wire-key tolerance
boundary, untrusted-input handling) and rewords G05's XMPP and core-authorized
LXMF replay legs as dated one-time provenance — only the required
`core-owned-lxmf-integration` CI job self-refreshes at the current pins.
