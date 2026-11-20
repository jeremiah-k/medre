---
name: medre-core-development
description: Implement or diagnose behavior changes in MEDRE core. Use for config and route semantics, runtime or storage behavior, shared provider contracts, and core dependency or compatibility changes.
---

# Develop MEDRE core

## Trace the change to its owner

State the expected behavior at the CLI, runtime, or provider boundary.
Follow that entry point to the code that makes the decision. Use the
applicable spec to identify the invariant and find a regression case that
would distinguish the old behavior from the expected behavior.

- **Config or routing**. Follow the loader and environment overrides in
  `src/medre/config`, then route compilation and matching. Check the offline
  CLI and runtime paths when both consume the same decision. Start from the
  [config spec](../../docs/spec/configuration.md) or
  [routing spec](../../docs/spec/routing-delivery.md).
- **Delivery, persistence, or recovery**. Trace the durable record that
  authorizes work and the projection used to report it. Test at the affected
  failure boundary, such as a retry or storage reopen. Use the
  [delivery lifecycle](../../docs/spec/delivery-lifecycle.md) and the relevant
  [storage](../../docs/spec/storage.md) or
  [evidence](../../docs/spec/diagnostics-evidence.md) contract.
- **Provider contract**. Keep generic orchestration in core and native
  addressing, SDK handling, and protocol behavior in the provider. See
  [adapter runtime](../../docs/spec/adapter-runtime.md) and
  [external adapters](../../docs/dev/external-adapters.md).
- **Dependencies, extras, or package artifacts**. Sync the locked environment
  with all required extras before using `--no-sync`. Check the installed
  entry point or built artifact when source imports alone cannot verify the
  change. See the [environment guide](../../docs/dev/environment.md).

If a spec and implementation disagree, establish the intended behavior from
the task and existing contracts before changing either. For persisted or
published behavior, check older data and callers. Consult the
[versioning policy](../../docs/dev/versioning-policy.md) before changing
schema assets. Preserve the frozen release bytes.

## Prove the behavior at the right boundary

- Exercise the normal entry point or the lowest layer that owns the invariant.
  When the risk is wiring or restart behavior, include a test that reaches it
  through that path. Use deterministic lifecycle hooks for races.
- Keep generic regression tests runnable with `pytest --core-only`. Declare
  provider-dependent modules as compatibility tests using the
  [test-tier guidance](../../docs/dev/testing.md#adapter-free-and-compatibility-ownership).
  Put provider-native regressions in the provider repository.
- For a shared contract change, qualify the exact core/provider pair.
  Candidate pair results describe those revisions. The committed compatibility
  manifest takes landed provider commits. Follow
  [coordinated changes](../../docs/dev/coordinated-changes.md) for the pin and
  merge sequence.
- Match the claim to the test. A fake smoke run exercises the core pipeline.
  SDK and live tests have separate prerequisites. Keep native handoff,
  provider acceptance, and independent recipient observation distinct when
  reporting delivery evidence.

Update the relevant spec and regression coverage with a semantic change.
Update the operator procedure when a command or its output changes. Run the
focused tests and the applicable Trunk and typing checks from
[AGENTS.md](../../AGENTS.md). Report the checks actually run and any relevant
gated tier that remains unverified.
