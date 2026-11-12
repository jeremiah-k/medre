# MMRelay legacy compatibility removal

Remove the mmrelay legacy compatibility framing from the operator and spec
surface: operator docs, example configs, and the live-validation journey no
longer reference the option, spec sections describing MEDRE's own legacy flat
`meshtastic_*` Matrix content emission are rewritten to the native framing,
and native origin-label coverage stops setting the flag. The trust boundary
that rejects foreign MMRelay wire fields as untrusted participant metadata is
unchanged. Together with the Meshtastic adapter revision that drops the
`mmrelay_compatibility` field, the schema key, its example occurrence, and the
frozen schema hash leave `adapter-config.schema.json` atomically, keeping the
schema-source drift contract satisfied. `medre.interop.mmrelay` remains until
no adapter imports it.
