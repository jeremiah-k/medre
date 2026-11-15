# Qualify core-scoped Meshtastic daemon-local replay

Add a Docker-gated exact-target replay suite using the manifest-pinned
Meshtastic adapter, real `mtjk` session and containerized `meshtasticd` daemon.
Ordinary single-packet replay covers deferred queue admission and later durable
feedback; planned multipart replay covers existing synchronous per-part grants.
Both exclude same-adapter channel and separate-adapter siblings. Missing
persisted generations and wrong-channel requests refuse before the SDK send.
Stale preview re-derivation also fences the prior claimed worker before SDK use.

Required Docker CI enforces five executed cases, rejecting skipped, missing or
failed tests, and uploads its executed JUnit report for 90 days. This does not
change runtime send authority, provider pins or storage schema. A local SDK
packet ID does **not** prove independent daemon acceptance, RF delivery or
recipient acknowledgment. No uncertainty authorizes resend.
