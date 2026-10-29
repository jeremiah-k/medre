# Inspect exact cancellation decisions

For a previously admitted event, inspect immutable core cancellation evidence:

```bash
medre inspect decisions --event EVENT_ID --storage-path /private/medre.sqlite
```

This uses strict SQLite read-only access. Output conforms to `operator-decision-inspection.schema.json` (version 1).
It includes `decision_id`, the
exact destination, the observed attempt generation, status and
`observed_updated_at` timestamp (distinct from decision `recorded_at`), and
an **unverified** actor label. `actor_labels_authenticated` is always `false`:
the label identifies what the initiating caller recorded, not an authenticated
principal. If there are no decisions, `decisions` is an empty array; an unknown
event exits with the not-found code.

This command is **not** a cancellation or replay command. Previewing a delivery
fingerprint cannot authorize mutation. The existing storage authority only
cancels a currently unreserved, matching generation; uncertain or accepted
native sends remain fenced. Do not attempt manual database updates or equate a
cancelled unsent obligation with delivery confirmation.

Inspection accepts every non-empty canonical event identifier. Cancellation
request bounds do not restrict read-only inspection of an existing event.
