# Provider wrapper and policy validation parity

Adapter-owned runtime wrappers are validated through their lifecycle fields,
without requiring inheritance from core's generic fallback. This preserves
independent provider configuration models while retaining strict adapter ID,
enabled flag, and implementation-kind validation.

Direct bridge-policy construction rejects null, non-sequence, and non-string
allowlists, and copies list inputs into immutable tuples. Both routing and
runtime configuration schemas now reject explicit null policy, retry, and
context-map blocks, matching the YAML loader.
