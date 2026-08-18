# Matrix recovery failure evidence

- Add crash-window tests proving failed atomic admission leaves no partial canonical
  state, admitted work survives restart, stale worker leases are reclaimed, and
  re-decoded Matrix native events retain their original canonical identity.
- Preserve recovery-abandonment causes with the committed Matrix checkpoint before
  settling nio's degraded-room marker, and expose the evidence through diagnostics.
- Expose durable-ingress worker running/processed/failure counters in runtime
  diagnostics.
