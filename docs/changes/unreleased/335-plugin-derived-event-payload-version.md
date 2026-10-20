# Explicit Plugin-Derived Payload Schema Versions

Require every typed derived-event proposal to have an independent positive
payload schema version, defaulting to the current v1 contract. The host must
not copy the source event's schema version when deriving a different event
kind or payload. Include the proposal version in immutable decision evidence;
advance the closed plugin execution record envelope and schema to v2.

This is a pre-release plugin contract change, with no legacy fallback.
