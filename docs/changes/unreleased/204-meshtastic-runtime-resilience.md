# Meshtastic runtime resilience

- Add TCP-only active round-trip liveness supervision using the pinned mtjk metadata-request callback contract, with generation-safe recovery and explicit response-handler cleanup on timeout.
- Replace MEDRE's finite ten-attempt Meshtastic session reconnect ceiling with lifetime client recreation bounded by configurable exponential-backoff delays and adapter shutdown.
- Make the Meshtastic outbound queue capacity configurable and add warning/critical/full pressure states, peak-depth diagnostics, and health degradation before hard-cap rejection.
- Keep low-level mtjk transport heartbeat/reconnect behavior SDK-owned; no Meshtastic recovery policy is moved into generic MEDRE runtime/core supervision.
