# Wire inbound capacity limits through YAML and validate resource options

Accept `runtime.limits.max_inflight_inbound_admissions` and
`runtime.limits.inbound_admission_timeout_seconds` in YAML, in line with the
published runtime schema and durable ingress contract. Apply those values to
`RuntimeLimits` so the capacity controller uses the operator's configured
bounds rather than the defaults.

Reject invalid concurrency and timeout values (including booleans, non-finite
floats, and mismatched types) at configuration load rather than leaking them
into asyncio primitives. Validate the top-level runtime name and shutdown
timeout. Add schema/model/parser parity tests so future limits cannot become
documented-but-unconfigurable fields again.
