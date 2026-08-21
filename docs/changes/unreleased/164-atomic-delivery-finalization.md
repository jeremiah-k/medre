# Atomic queued delivery finalization

- Finalize delayed queue-backed sends with one storage transaction that couples
  the outbound native-message reference, immutable `sent` receipt, and exact
  outbox attempt transition to `sent`.
- Revalidate the outbox ID, attempt number, and non-terminal queue state inside
  the transaction so a stale callback cannot partially commit delivery evidence.
- Reject conflicting native identities that already map to a different canonical
  event instead of recording contradictory delivery evidence.
- Preserve the adapter callback boundary as fact reporting: adapters return the
  native send result while core owns durable delivery lifecycle transitions.
