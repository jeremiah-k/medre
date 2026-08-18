# Durable ingress storage foundation

- Add atomic canonical-event/native-reference/work admission for inbound events.
- Add persisted application-owned adapter checkpoints.
- Define LIVE/RECOVERED/HISTORY provenance and durable ingress guarantees.
- Reject corrupt persisted provenance or work status during duplicate admission.
