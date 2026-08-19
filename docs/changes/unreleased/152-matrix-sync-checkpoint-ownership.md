# Matrix Classic Sync checkpoint ownership

- Move runtime-managed Matrix adapters to mindroom-nio Classic `sync_forever()` with
  bounded SDK request retries and MEDRE-owned outer supervision.
- Enable limited-timeline recovery with application-owned Classic checkpoints and
  durable `LIVE`/`RECOVERED`/`HISTORY` admission semantics.
- Persist Matrix cursor and recovery-abandonment evidence in MEDRE storage before
  acknowledging the staged Classic response to mindroom-nio.
- Reject failed durable admissions at nio's admission boundary so replay remains
  possible after storage errors or crashes.
- Enable the durable callback trio only when runtime storage is available; otherwise
  leave Classic checkpoint ownership with mindroom-nio.
