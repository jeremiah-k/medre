# Matrix event normalization

- Define a versioned Matrix-native metadata namespace for stable sender, room,
  event, timestamp, relation, relay, media, and encryption provenance.
- Normalize Matrix edits, redactions, threads, and media events through the
  existing transport-neutral event kinds and relation vocabulary.
- Register Matrix media and redaction event classes at the session boundary and
  retain only safe decryption provenance, excluding crypto key/session material.
- Isolate scalar MMRelay compatibility fields under `native.interop.mmrelay` while
  leaving the established cross-transport relation metadata contract unchanged.
- Publish a standalone JSON Schema for the Matrix-native metadata contract so
  external producers can implement the shape without importing MEDRE runtime.
- Matrix relation classification (reply/edit/thread/redaction) now takes precedence
  over MMRelay emote-reaction markers when an event carries both; MMRelay reactions
  without a Matrix relation are unaffected.
