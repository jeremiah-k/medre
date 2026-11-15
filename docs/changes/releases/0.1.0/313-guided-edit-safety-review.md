# Guided route edits and offline explanations

- Validate an existing route's supported guided-edit shape and fixed endpoints
  before collecting interactive context pairs; advanced routes are rejected
  rather than entering an unfinishable retry loop.
- Default interactive replacement to the existing route direction, preserving
  one-way traffic restrictions unless the operator explicitly changes it.
- Suggested native-setup follow-up commands use the actual saved configuration
  path and propose an unused destination filename.
- Route explanations keep reasons aligned with the production matcher and
  distinguish structured target kinds in text output. They remain offline
  diagnostics and do not promise native delivery.
- Correct provenance references and credential handling guidance.
