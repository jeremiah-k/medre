# Bounded inbound admission

- Gate every inbound admission crossing — `publish_inbound` and
  protocol-provenance `admit_inbound` — behind a runtime-wide bounded
  semaphore, so the per-SDK-callback ingress coroutines of the radio
  transports (Meshtastic, MeshCore, LXMF) can no longer commit ingress
  with unbounded concurrency.
- Reject arrivals that overflow the wait queue (bounded at the admission
  limit itself), wait past `inbound_admission_timeout_seconds`, or arrive
  after inbound acceptance closed, with the typed
  `InboundAdmissionRejected`; adapters log the rejection as counted
  ingress loss rather than dropping it silently.
- Extend runtime limits with `max_inflight_inbound_admissions` (default 100) and `inbound_admission_timeout_seconds` (default 5.0), including
  YAML validation, `MEDRE_RUNTIME_*` environment overrides, the published
  runtime configuration schema, and the evidence-bundle limits section.
- Surface inbound acceptance, wait depth, oldest pending wait age, and
  rejection and timeout counters in the capacity controller snapshot used
  by runtime metadata and operator diagnostics; per-arrival wait
  timestamps keep the oldest-wait age accurate as waiters leave.
- Split shutdown acceptance: delivery and replay stop taking work before
  the capacity drain as before, while inbound stays admissible through
  adapter teardown so late callbacks persist rows for the next runtime
  generation; inbound closes after the last adapter stops and outstanding
  admissions drain before the pipeline runner and storage close.
