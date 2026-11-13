# Unify the radio adapter relay prefix configuration before the first release

Meshtastic and MeshCore target adapters now use the same `relay_prefix` key as
other configurable renderer targets. The core YAML examples, environment
configuration examples, lab provisioner, adapter-config JSON schema, and
cross-adapter tests use the installed adapters' current dataclass fields. No
old-name alias is accepted by either adapter.

Meshtastic retains three meanings: `null`/omitted selects automatic attribution,
`""` explicitly disables the prefix, and a nonempty string supplies a custom
template. MeshCore's default `""` remains disabled. Both constrained-display
renderers permit `{senderN}`, `{sender_shortN}`, and `{origin_labelN}` for N >= 1.

This pre-first-release operator-schema correction intentionally updates only the
`adapter-config.schema.json` digest in `v0.1.0.freeze.json`. The canonical event
and bridge-context wire formats remain byte-for-byte unchanged. The adapter pins
are promoted with this change: Meshtastic
`25b5583b2caae3b699f3fdff59b470b29e6eadcd` and MeshCore
`040cbfe39eead9bdc18a784abb7eae07706034cd`. Rerun core schema, docs-drift,
cross-adapter and live campaign checks before accepting the final first-release
schema snapshot.
