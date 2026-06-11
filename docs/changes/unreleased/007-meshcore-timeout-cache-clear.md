# MeshCore Per-Contact Retry Timeout Cache Clear

Clear MeshCore per-contact retry timeout cache on reconnect and failed-start cleanup.

## Changed

- `src/medre/adapters/meshcore/session.py`: `_cleanup_failed_start()` now clears
  `_contact_retry_delays` alongside subscriptions and SDK client, preventing stale
  timeout hints from surviving a failed start.
- `src/medre/adapters/meshcore/session.py`: `_reconnect_loop()` now clears
  `_contact_retry_delays` immediately after `_connect_real()` succeeds, ensuring
  the new connection does not carry over timeouts from a previous connection's
  radio conditions or hop count.

## Added

- `tests/test_meshcore_retry_timeout_cache.py`: five test cases verifying cache
  clearing on `stop()`, failed-start cleanup, successful reconnect, per-contact
  isolation, and channel-send independence from the DM timeout cache.
