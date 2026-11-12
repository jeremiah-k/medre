# Promote the Matrix adapter pin

Promote the Matrix adapter that drops the MMRelay interop surface: outbound
events no longer carry the legacy flat `meshtastic_*` fields, reaction bodies
carry no stray leading newline, and a reaction whose target resolves in-room
renders as a native Matrix annotation with a quoted fallback otherwise. The
embedded canonical payload is unchanged and stays within the frozen 0.1.0
schemas.
