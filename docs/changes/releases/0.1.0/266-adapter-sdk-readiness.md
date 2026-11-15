# Accurate optional adapter SDK readiness

Use each installed adapter's declared dependency probe for operator SDK
availability, rather than considering any one optional import sufficient.
This prevents an incomplete LXMF/Reticulum installation from being reported
as usable. Clarify that `medre adapters check` only verifies entry-point
installation, while `medre adapters` reports adapter-owned SDK availability.

Distinguish missing runtime configuration from invalid/unreadable configuration
in the adapter inventory without echoing potentially sensitive validation data.
