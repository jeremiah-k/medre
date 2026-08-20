# Prerelease contract consolidation

- Standardize built-in canonical native metadata on one versioned namespace per
  transport: `native.matrix`, `native.meshtastic`, `native.meshcore`, and
  `native.lxmf`; keep MMRelay wire interoperability isolated under
  `native.interop.mmrelay`.
- Remove readers and construction paths that existed only for abandoned MEDRE
  development shapes, while retaining external protocol, SDK, and current
  operational compatibility boundaries explicitly documented by the spec.
- Make `channel_room_map` structured-only, with required `room` and optional
  per-entry origin labels; bare room-ID entries are rejected.
- Define the canonical event envelope as closed and versioned, with extension
  data preserved through `payload`, `metadata.custom`, and versioned native
  namespaces rather than unknown top-level fields.
- Remove the unused canonical-event migration registry, alternate replay render
  hook, inline live-ingress fallback, flat smoke-report reader, and obsolete
  mixed real-adapter example configuration.
- Add machine schemas and examples for Meshtastic, MeshCore, and LXMF native
  metadata plus a generated current-state inventory that checks source/schema
  version authority and marks developer audits as historical snapshots.
