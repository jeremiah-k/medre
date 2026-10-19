# Distinguish canonical and transport-private persistence

Clarify that the single-backend requirement applies to MEDRE-owned canonical
storage. Private protocol stores required by external SDKs (including Matrix
E2EE) remain in the adapter's private state root, outside the MEDRE event and
outbox database. Document stdout-only logging instead of an implied log file.
