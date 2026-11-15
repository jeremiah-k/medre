# Return adapter development installs to main

- Add `medre adapters reset [ALIAS...] [--dry-run]` to restore selected installed
  first-party adapter sources, or all installed first-party sources, to main.
- Remove the candidate `integration` extra while retaining ordinary extras,
  the core request, community adapters, and unrelated package requests.
- Reuse uv-tool ownership, complete-request planning and Windows handoff. Saved
  source catalogs remain available; no runtime state or credentials are edited.
