# Own SQLite connections through cancelled initialization

Retain in-flight read-write and read-only connection openers until their worker
settles. Adopt a returned connection before propagating cancellation so failed
initialization closes both the native handle and its private executor. Keep
caller cancellation counts and retrieve worker failures during rollback.
