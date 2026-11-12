# MMRelay legacy compatibility removal

Remove the mmrelay legacy compatibility framing from the operator and spec
surface: operator docs, example configs, and the live-validation journey no
longer reference the option, spec sections describing MEDRE's own legacy flat
`meshtastic_*` Matrix content emission are rewritten to the native framing,
and native origin-label coverage stops setting the flag. The trust boundary
that rejects foreign MMRelay wire fields as untrusted participant metadata is
unchanged. `mmrelay_compatibility` stays declared in
`adapter-config.schema.json` and on the pinned Meshtastic adapter for now: the
schema-source drift contract couples the published schema to the installed
adapter dataclass, so the key leaves the schema in the sequenced change that
drops the adapter field, together with retiring `medre.interop.mmrelay` once
no adapter imports it.
