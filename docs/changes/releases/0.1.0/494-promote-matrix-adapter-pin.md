# Promote the Matrix adapter pin

Promote the Matrix adapter revision that drops the MMRelay interop surface:
outbound events no longer carry the flat `meshtastic_*` wire fields, a
reaction whose target resolves in-room renders as a native Matrix annotation
with a quoted emote fallback otherwise, and the fallback body keeps exactly
one intentional newline after any configured relay prefix. The embedded
canonical payload is unchanged and stays within the frozen 0.1.0 schemas.

The core compatibility suite migrates to the canonical renderer interfaces:
shared fixtures construct the renderer through its canonical constructor,
pipeline suites stop passing the retired `source_configs` surface, sender
identity resolves through the registry-driven attribution projectors, and
coverage locking the retired wire-field emission, the removed reaction-symbol
fallback chain, and the compat-forced emote path is removed with the surface
it locked. The displayname trust boundary and the structural mmrelay
isolation guards keep their coverage under the canonical paths.
