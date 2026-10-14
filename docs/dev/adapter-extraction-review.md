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

These slices overlap; the counts are not a single-suite total. Full core collection also
succeeds: 14,047 selected cases, 69 gated cases deselected. The final
registry/dispatch/artifact/CI interface slice passed 86 checks, and all four native SDK
environments pass dependency consistency checks. Lint, formatting, compilation, workflow
validation, whitespace checks, and the offline core lock freshness check are recorded
with the final verification. The entire core regression suite was not executed.

Hardware, external live services, soak, macOS/Windows execution, and remote
GitHub Actions runs remain unverified locally. Core CI is configured to run its
independence checks on Linux, macOS, and Windows; SDK qualification is Linux.

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
