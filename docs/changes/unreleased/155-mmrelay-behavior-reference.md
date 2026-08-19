# Changed

- Formalize MMRelay as a non-runtime behavioral reference for Matrix and Meshtastic
  edge cases, with executable MEDRE requirements for authenticated device discovery,
  E2EE peer-device rotation, bounded missing-room-key recovery, stale radio callbacks,
  SDK connection health, shutdown ordering, and native reply construction.
- Detach live Megolm recovery retries from nio sync callbacks, cancel tracked recovery
  tasks during shutdown, and classify permanent Matrix errcodes without retry.
- Serialize Meshtastic client replacement with reader-thread callback validation and
  revalidate connection generations before publish or reconnect work runs.
