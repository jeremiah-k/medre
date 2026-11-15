# Typed setup instructions for form-less providers

Add an optional, informational `AdapterSpec.native_setup_instructions`
declaration (`NativeSetupInstructions(title, summary, steps)` of ordered
`NativeSetupStep(command, purpose, evidence_note)` values) so a provider
whose provisioning cannot be an offline form can declare its own
machine-readable setup journey. Each step carries the verbatim provider
command, a short purpose, and an evidence note stating what the completed
step proves and does not prove: a completed provisioning step is never relay
acceptance, delivery, or read evidence and grants no send authority.

`medre config native` now renders that journey offline for an
instructions-only provider with an explicit `informational_only`
classification instead of the provider-declared-prompts refusal. Core never
executes a declared command (they contact native services), writes no file,
fabricates no configuration, and claims no provisioning occurred. Providers
without instructions, providers with form fields, and the existing refusal
for providers with neither surface are unchanged; the declaration follows
the same optional-symbol fallback pattern as `native_setup_fields`, so
adapters can adopt it while keeping compatibility with older cores.

Informational rendering rejects C1 and Unicode direction controls, skips
provider CLI module loading, and does not expand an ignored output path.
