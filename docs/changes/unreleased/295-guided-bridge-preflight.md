# Guided bridge preflight and validation

Guided bridge authoring now checks provider-owned startup restrictions before
previewing or saving and rejects overlapping context-less text routes. Error
redaction preserves benign field values and words containing short secrets.
Failed descriptor setup closes and cleans up its private output, and interrupted
saves no longer claim that no file was written. Empty drill names cannot bypass
normal-smoke option guards. Direct route construction enforces the complete
route-ID grammar; the runtime schema rejects unsupported entry-level `route_id`
fields. Guidance clarifies loop warnings, fan-in direction, and scripted saving.
