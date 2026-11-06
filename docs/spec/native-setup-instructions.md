# Native Setup Instructions

This page specifies the typed, informational setup-instructions seam for
form-less providers. It extends the adapter registration metadata defined in
[adapter-runtime.md](adapter-runtime.md) without changing provisioning or
send authority anywhere in the runtime.

## Authority

The seam is **informational only**. Declaring setup instructions
**MUST NOT** become provisioning authority, send authority, or evidence of
any runtime outcome. Core **MUST NOT** execute a declared command, because
provider setup commands contact native services that only the operator may
drive. A completed provisioning step is never relay acceptance, delivery, or
read evidence and grants no send authority.

## Declaration

A provider **MAY** declare `native_setup_instructions: NativeSetupInstructions | None`
on its `AdapterSpec` when its provisioning cannot be an offline form. The
declaration is SDK-free, frozen metadata with the same optional-symbol
semantics as `native_setup_fields`: absence changes nothing, and providers
declaring both keep the guided form authoritative.

A declaration consists of a `title`, a `summary`, and an ordered, non-empty
tuple of `NativeSetupStep` values. Each step carries:

- `command` — the verbatim provider tooling invocation, exactly as the
  provider contributes it. Core renders the command without interpretation.
- `purpose` — a short description of what the step does.
- `evidence_note` — a statement of what observing the completed step proves
  and what it does **NOT** prove.

Declarations **MUST** be deterministic: they **MUST NOT** embed secrets,
building-machine paths, or timestamps. Step order **MUST** match the
provider's documented journey order. Core **MUST** reject a malformed
declaration (wrong types, an empty journey, empty step text, terminal control
characters (including C1 controls), line/paragraph separators, or Unicode
bidirectional text controls) instead of rendering it. This
validation is a terminal-display boundary, not proof that a provider-owned
command is safe or correct to run.

## Offline rendering boundary

For a provider that declares instructions and no `native_setup_fields`, the
`medre config native` surface **MUST** render the declared journey offline
with an explicit informational classification (`informational_only`) instead
of the provider-declared-prompts refusal. The rendered output **MUST**:

- state that the commands are provider-owned, run manually by the operator,
  and never invoked by core because they contact native services;
- state that completing a step never proves relay acceptance, delivery, or
  read evidence and never grants send authority;
- warn that provider-declared `--adapter` values are examples and MUST be
  replaced with the intended configured instance before manual use;
- state that no configuration file was written;
- be deterministic and derived only from the declaration and the transport
  name (no clocks, no operator paths).

Rendering **MUST NOT** fabricate a configuration file, **MUST NOT** write
any file, and **MUST NOT** claim that provisioning occurred. The existing
refusal semantics are preserved for every other case: a provider with
`native_setup_fields` keeps the guided form, and a provider with neither
fields nor instructions keeps the provider-declared-prompts refusal.

Providers without instructions are unaffected: discovery, readiness
reporting, and all other surfaces behave exactly as before.
