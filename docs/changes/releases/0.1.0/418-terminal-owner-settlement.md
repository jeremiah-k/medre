# Transactional terminal content-owner settlement

- Guarded ingress completion atomically releases its exact source hold after
  durable fanout adoption. Pending siblings retain independent target grants.
  Replay of a normally completed event is therefore denied content and reports
  the named `replay_content_authority_denied` skip outcome, not a failure.
- Terminal outbox transitions and receipt/native-reference finalization settle
  matching target ownership in the same transaction. Stale or repeated updates
  never settle another owner's content. Native evidence remains intact.
- A failed settlement write rolls back the lifecycle transition and ownership
  release together, preserving the public `StorageError` contract.
