# Optional runtime status heartbeat

Add `medre run --status-file PATH` for read-only external service monitoring.
The content-free, atomically replaced JSON file distinguishes process lifecycle,
retained/startup health, observation time and startup route counts. It does not
open transport connections or modify delivery authority. Consumers must reject
stale or unsupported data. Without the flag, the normal run path is unchanged.
