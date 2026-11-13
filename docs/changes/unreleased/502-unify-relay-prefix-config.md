# Unify the radio adapter relay prefix configuration before the first release

Meshtastic and MeshCore target adapters now use the same `relay_prefix` key as
other configurable renderer targets. The core YAML examples, environment
configuration examples, lab provisioner, adapter-config JSON schema, and
cross-adapter tests use the installed adapters' current dataclass fields. No
old-name alias is accepted by either adapter. Before upgrading, rename Meshtastic
`radio_relay_prefix` and MeshCore `meshcore_relay_prefix` to `relay_prefix`.
The matching environment overrides become
`MEDRE_ADAPTER__<TOKEN>__RELAY_PREFIX` instead of
`MEDRE_ADAPTER__<TOKEN>__RADIO_RELAY_PREFIX` or
`MEDRE_ADAPTER__<TOKEN>__MESHCORE_RELAY_PREFIX`. The enclosing transport
section still selects the adapter; there is no runtime compatibility shim.

Meshtastic retains three meanings: `null`/omitted selects automatic attribution,
`""` explicitly disables the prefix, and a nonempty string supplies a custom
template. MeshCore's default `""` remains disabled. Both constrained-display
renderers permit `{senderN}`, `{sender_shortN}`, and `{origin_labelN}` for N >= 1.

The standalone adapter config schema is an untagged `anyOf`: its transport arms
can overlap on `relay_prefix`, and runtime configuration selects the exact arm
via `adapters.<transport>.<instance>` before binding a provider dataclass.
A `oneOf` union incorrectly rejected valid overlapping shapes.

This pre-first-release operator-schema correction intentionally updates only the
`adapter-config.schema.json` digest in `v0.1.0.freeze.json`. The canonical event
and bridge-context wire formats remain byte-for-byte unchanged. The adapter pins
are promoted with this change: Meshtastic
`25b5583b2caae3b699f3fdff59b470b29e6eadcd` and MeshCore
`040cbfe39eead9bdc18a784abb7eae07706034cd`. Rerun core schema, docs-drift,
cross-adapter and live campaign checks before accepting the final first-release
schema snapshot.
