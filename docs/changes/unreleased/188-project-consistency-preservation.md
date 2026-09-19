# Project-consistency preservation follow-up

## Audit pruning

- Reconcile the consolidated prerelease history with the current strict
  contracts: `channel_room_map` is structured-only, per-entry origin labels
  are supported for mapped channels, and built-in Meshtastic native metadata
  is read from the versioned `native.meshtastic` namespace rather than bare
  adapter-native fields.
- Restore the release-readiness provenance guard that was accidentally removed
  with historical audit-document checks. The focused guard pins the recorded
  MeshCore/LXMF local-integration status and the historical tree/workflow
  evidence without restoring deleted audit snapshots or generated inventory.
- Clarify the routing specification so the generic origin-label precedence
  includes the per-entry level for `channel_room_map` legs.

No runtime behavior changes are introduced by this follow-up.
