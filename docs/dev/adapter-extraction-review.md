# Adapter extraction review

## Scope and decisions

Reviewed the extraction branch from core merge-base
`7958a22a05ceeea8e684e59c9d99d82bbf53cf99`, then applied PC's October 14 ownership
patches after verifying all five baseline commits and patch checksums. The
result spans MEDRE and the four sibling adapter repositories, preserving
existing history and attribution.

The runtime seam uses installed Python entry points and lazy declarations.
Immutable native-detection precedence is computed once rather than sorted per event.
Adapters own native behavior, config, fakes, SDK requirements, native addressing,
and protocol tests. Core owns generic contracts and orchestration. Distribution
names, import packages, repository hosts, and transport names are independent.
The first-party namespace layout is optional for community authors.

No radio category is required. Privacy-oriented, decentralized chat platforms
and proprietary integrations use the same contracts; no new platform adapter
was implemented in this change.

## Standards review: fixed findings

- Removed the temporary named-wrapper import facade, historical built-in
  registry name, preferred transport order, and obsolete radio trait/category.
  Tests import the owning distribution's wrapper directly.
- Moved 153 single-transport test modules and their required local helpers into
  adapter repositories. Cross-adapter native projection, YAML bridge fixtures,
  evidence collection, and live harness checks remain in core's compatibility
  suite. No adapter depends on MEDRE's private test package.
- Replaced architecture checks that silently skipped extracted files or assumed
  core-relative paths with installed-source resolution. Resource-control checks
  scan the installed namespace contributors as well as core.
- Pruned the core lockfile from 80 resolved packages to 23; native SDK graphs
  are absent. SDK pin checks use installed adapter metadata and standard
  requirement/marker parsing.
- Corrected setup documentation and stale tests expecting removed core extras.
  Added standalone adapter CI and preserved explicit SDK/local/soak gates.

## Spec and adversarial review: fixed findings

- Static schemas rejected community transports and destination kinds. They now
  validate extensible structure; installed config models and pure
  `DestinationSpec` validators enforce provider rules.
- Native address requirements lived in core. Matrix room, MeshCore contact,
  and LXMF hash validation now live with their adapters. Unknown and conflicting
  addressing declarations fail explicitly.
- A valid address for one provider could be attached to another provider's
  target. Loading and runtime assembly reject mismatches before construction,
  including addresses nested in `context_map` entries.
- Smoke chose Matrix by platform. It now chooses a configured route source.
  Lifecycle drills use installed fake providers instead of fixed transport types.
- Recovery inferred delivery semantics from radio labels and adapter-name
  prefixes. Duplicate-send guidance now uses recorded outbound native references
  and retryable failure evidence, regardless of platform or installed providers.
- Adapter preflight accepted empty lists/strings/false/zero as an empty mapping.
  These inputs are rejected. Adapter `enabled` values require actual booleans.
- SDK/local CI originally collected core tests that imported uninstalled
  providers. Tests now run in their owning repository, and core independence has
  its own job. Compatibility jobs consume one resolved commit snapshot per run;
  SDK and local tier selection is data-driven and soak remains opt-in.
- The installed-wheel proof still expected bundled providers. It now verifies
  core alone, then optionally installs external wheels, checks config/wrapper/
  fake module provenance, and proves the sample bridge without source checkouts
  or native SDKs.
- A deleted module survived in setuptools' old `build/` directory. Clean builds
  remove generated artifacts, and the wheel proof now rejects Python package
  files missing from or absent in the current source tree.

## Verification

All local execution used Linux. Python async/thread shutdown stalled under the
restricted executor; an independent minimal reproduction isolated that executor
issue, and affected checks were rerun with normal local permissions.

| Check                                                             | Result                  |
| ----------------------------------------------------------------- | ----------------------- |
| Real adapter-free core environment                                | 303 passed              |
| Matrix alone with core, no SDKs/other adapters                    | 1,056 passed, 1 skipped |
| Meshtastic alone with core, no SDKs/other adapters                | 1,111 passed            |
| MeshCore alone with core, no SDKs/other adapters                  | 739 passed              |
| LXMF alone with core, no SDKs/other adapters                      | 772 passed              |
| Pinned SDK contracts, all four adapters                           | 62 passed               |
| LXMF and MeshCore deterministic local integration                 | 13 passed               |
| Affected config/environment/architecture slice                    | 719 passed              |
| Targeted review, CLI, recovery, and source-resolution checks      | 378 passed              |
| Direct/context-map address ownership and strict preflight slice   | 91 passed               |
| Five clean wheels; isolated core plus external-wheel bridge proof | Passed                  |

These initial extraction slices overlap; the counts are not a single-suite total.
The original full core collection succeeded: 14,047 selected cases, 69 gated cases deselected. The final
registry/dispatch/artifact/CI interface slice passed 86 checks, and all four native SDK
environments pass dependency consistency checks. Lint, formatting, compilation, workflow
validation, whitespace checks, and the offline core lock freshness check are recorded
with the final verification. The entire core regression suite was not executed locally.

Publishing qualification runs in [PR #265](https://github.com/jeremiah-k/medre/pull/265).
The initial remote regression exposed 17 remaining obsolete assertions: bundled
source locations, removed core SDK extras, a ban on namespace extension, and two
fixtures attaching LXMF addresses to Matrix targets. Targeted corrections pass
509 checks, retaining the new target validation and scanning installed adapter
sources. The Docker runner also passed an empty pytest path, collecting unrelated
suites; positional argument forwarding now keeps collection within its integration
tier and has regression coverage.

All four independent adapter workflows passed. Core independence and installed-wheel
checks passed on Linux, macOS, and Windows; SDK qualification and deterministic
Docker integration run on Linux. Hardware, external live services, and soak remain
unexecuted. The PR checks record the current full regression result.

## Follow-up review and smoke evidence

The follow-up review covered core commits after the initial extraction and the
recent fake-event-factory changes in all four adapter repositories. Saved smoke
artifacts showed the earlier wrong-target drill failures and the later passing
drill session. The saved live snapshot reported a stopped runtime, three accepted
events, six outbound handoffs, and zero outbound failures; those process-local
counters do not establish recipient delivery.

Targeted regressions reproduced and fixed incomplete fanout passing after adapter
construction/startup failure, drill selection ignoring channel/event-kind filters,
and an event-factory exception escaping runtime cleanup. Smoke sources now require
successful startup and the optional factory. Local pin verification accepts
normalized distribution names and removes temporary files after failed writes.
Adapter documentation uses the generic smoke contract. The bounded follow-up
slice passed 78 tests; Ruff, Black, and compilation passed on 24 recent Python
files. No broad suite or hardware session was rerun locally for those fixes.

The initial published extraction completed full core regression across Python
3.11–3.14 with 13,992 passing cases per interpreter at that revision, plus core
independence on Linux/macOS/Windows, adapter SDK/local contracts, and Docker
integration. [PR #265](https://github.com/jeremiah-k/medre/pull/265) records the
current revision's CI status; the counts above describe the recorded checks,
not a claim that later documentation commits were rerun locally.

### Synthetic drill attribution correction

External review identified positional cycling that assigned an LXMF provider to
an instance named `fake_matrix`. Programmatic lifecycle drills now use neutral
IDs and explicit instance-to-transport maps, and report
`coverage_scope: synthetic_adapter_instances` with a cross-transport limitation.
They deliberately use one installed fake provider. Targeted tests isolate each
first-party provider and verify three separately constructed startup instances;
`adapters_total: 3` correctly counts instances rather than transport types.
With no installed provider, the CLI returns a structured failed report.

The bounded correction slice passed 59 tests, including report-schema checks,
explicit mixed assignments, and core-only CLI failure. Native addressing still
requires its owning provider, consistent with adapter-owned validation.

## October 14 follow-up review

The later adversarial report identified concrete gaps beyond relocation. All
reported functional issues were addressed:

| Finding                                                    | Resolution                                                                                                                                                              |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Run-session retained Matrix-first selection                | Share route-aware event/source helpers across smoke, sessions, and drills.                                                                                              |
| Native simulation methods accepted different input formats | Use the canonical `AdapterContract.publish_inbound` seam for session callbacks.                                                                                         |
| Providers without fakes produced misleading drill evidence | Filter optional fake declarations and verify construction before startup fault injection; verify the actual injected build exception.                                   |
| Short provider names shadowed longer names                 | Prefer explicit hints/native evidence, then exact and bounded longest ID matches.                                                                                       |
| Recovery scan assumed three radio transports               | Warn from unresolved delivery evidence; omit the warning for empty scans.                                                                                               |
| Generated samples required uninstalled providers           | Use adapter-owned native sample values/opaque addresses and core-assigned unique IDs; generate two instances for one provider or a loadable empty config with guidance. |
| Shared sample lists emitted forbidden YAML anchors         | Use safe, alias-free YAML serialization; prove every first-party provider alone.                                                                                        |
| One broken factory prevented help/preflight                | Quarantine failing/conflicting owners with retained diagnostics and actual provider distribution names.                                                                 |
| Core-only CI used a short positive allowlist               | Select all adapter-free modules by default with explicit compatibility ownership and a transitive import guard; separate provider-dependent helpers.                    |
| SDK extras had divergent CI consumers                      | Use manifest identity/extras for core contracts, Docker, and standalone adapter CI.                                                                                     |
| Compatibility planning used ambient Python                 | Select the declared Python before planning; execute through uv/the core interpreter.                                                                                    |
| SDK tests assumed distribution naming                      | Read core manifest identity or the adapter's own project metadata.                                                                                                      |
| Standalone results hid the core revision                   | Record exact tested SHA pairs; retain core's pinned pair as the qualification authority.                                                                                |
| Architecture audits omitted extracted source               | Add opt-in installed namespace/community source scans and a real-source inventory test; remove phantom shared-fake transports.                                          |
| Wheel/source mismatches and stale caches were confusing    | Explain untracked source/build leftovers and document scoped cache cleanup.                                                                                             |
| Core encoded native logger policy                          | Derive SDK defaults from registration; keep Matrix-specific policy in its provider and preserve operator overrides.                                                     |
| State-path calculation required installed providers        | Validate portable transport tokens independently of installation.                                                                                                       |

A negative cleanup test additionally exposed call-time stop-hook errors escaping
the supervisor. Invocation now occurs inside the deadline task, preserving
cleanup isolation and accepting Future-backed hooks. A lifecycle drill cannot
pass without observed failed-adapter cleanup.

The adapter-free tier was run once across its broad selection: 7,927 passed,
32 skipped, two expected failures, and 28 failures exposed remaining native
fixtures and generic assumptions about installed providers. Those findings
were corrected or given explicit compatibility ownership. A targeted rerun of
the affected core modules passed 292 checks; the broad tier was not repeated.
The complete compatibility suite still runs all modules in CI. Final core-only
collection succeeds across roughly 8,000 cases, with no undeclared import
coupling.

Follow-up slices passed 415 affected checks, 65 metadata/import contracts,
23 final provider contracts, and 140 serialization/discovery/logging checks.
These slices overlap. Another 50 schema/report checks and 27 run-session
checks passed. The final stop-invocation, deadline/cancellation, and lifecycle
evidence slice passed 58 checks. Each adapter's new standalone offline-sample regression
also passed. Formatting, lint, workflow checks, and final collection are
recorded with publication; hardware, live endpoints, Docker services, and soak
were not run locally during this follow-up.

## Release coordination

The curated CI manifest pins reviewed adapter commits for stability across
runs. Explicit branch/tag refs are also supported and resolved once per run.
Adapter workflows support a repository `MEDRE_CORE_REF` Actions variable and
a manual `core_ref` override for coordinated changes before core merges. Remote CI cannot exercise local working-tree edits;
matching core/adapter revisions must be published together. Deployment projects
should lock the complete chosen package graph separately from core's dev lock.

See [external adapter design and CI](external-adapters.md) for the resulting
ownership model. The packaging choices follow the
[Python entry-point specification](https://packaging.python.org/en/latest/specifications/entry-points/)
and the documented
[uv synchronization behavior](https://docs.astral.sh/uv/concepts/projects/sync/).
