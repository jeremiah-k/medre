# Four-target durable recovery regression

`tests/test_fourway_recovery_campaign.py` exercises SQLite ingress, routing,
outbox and retry ownership with four fake presentation targets. A failed
pre-handoff target leaves three completed targets unchanged. After stopping the
pipeline and reopening its database with new adapter instances, repeated native
ingress maps to the original canonical event. Only the unfinished target is
retried; completed outbox rows and receipt histories remain unchanged.

The test advances the retry worker's scheduling input to the retained deadline.
It does not sleep or edit scheduling records. The close/reopen boundary is a
graceful in-process reconstruction, not an operating-system crash. Adapter names
match a common mixed bridge, but every target uses the same SDK-free fake. This
regression provides no native BLE, pacing, signal handling, uncertain packet,
Matrix cursor or installation-restore qualification.
