# LXMF native worker and shared Reticulum setup guidance

Correct the LXMF operator guide to reflect the shipped native worker: a missing
explicit identity is created atomically in private storage and retained across
restarts, and separate worker profiles can attach to an existing shared
Reticulum service. Correct both real-mode config examples to supply the required
`storage_path`; distinguish native-worker ownership from the core process and
from the external service. No SDK, routing, or persistence behavior changes.
