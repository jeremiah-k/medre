# MMRelay legacy compatibility removal

Remove the `mmrelay_compatibility` option from the Meshtastic adapter config
surface: the published `adapter-config.schema.json` no longer declares the key,
the Meshtastic config example drops it, and operator-facing docs, example
configs, and the live-validation journey no longer reference it. Spec sections
describing MEDRE's own legacy flat `meshtastic_*` Matrix content emission are
rewritten to the native framing; compatibility-boundaries keeps the trust
boundary that rejects foreign MMRelay wire fields as untrusted participant
metadata. Deployments that still carry the key must drop it from Meshtastic
source configs; the boundary isolation and foreign-event handling coverage is
unchanged.
