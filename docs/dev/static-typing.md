# Incremental strict typing across MEDRE

This is a **migration**, not a claim that the entire project passes strict mypy.
The codebase supports Python 3.11+, so keep the checker pinned to the lowest
supported syntax/typing semantics while developing on newer interpreters.

## Source and execution model

Restore core and the seven independent adapter distributions as sibling
repositories. From the MEDRE core checkout run:

```console
uv sync --locked --extra dev
uv run --no-sync python scripts/check_strict_typing.py
uv run --no-sync python scripts/check_strict_typing.py --all
```

The pinned mypy tool ships in the locked `dev` extra; `msgspec` is a core
runtime dependency of every synced environment.

The first command is the currently passing gate; `--all` is a deliberately
**failing inventory** until every source tree is migrated. Both run the real
`mypy --strict` checker and analyze imported core symbols for adapters through
`MYPYPATH`, not by replacing MEDRE core with an untyped stub. Each repository
declares its typing semantics in its own `pyproject.toml` under `[tool.mypy]`:
the 3.11 target, strict mode, explicit package bases, silent follow-imports,
and per-module exemptions for exactly that repository's optional transport
SDKs, because those SDKs are not part of the offline typing toolchain. The
checker runs mypy from each repository root so the repository's own
configuration applies; repositories that have not declared the section get
equivalent flags with a generated exemption configuration. Any other
unresolved import — including a
misspelled MEDRE-owned module — fails the gate instead of degrading to `Any`.
Caller-provided `MYPYPATH` entries are kept after the workspace roots, so
external stub paths stay usable without shadowing the trees under check.
The SDK exemptions must be removed or
replaced with precise typed SDK boundaries before claiming full strict typing.
Do **not** silence local error codes globally, use `Any` to bypass a concrete
known type, or add broad per-module ignores to make this migration look complete.

`--workspace` can point to a different directory containing `medre` and the
adapter repos; the default is the core repository's parent. The gate requires
all seven adapter repositories to be present, and fails if one is
missing rather than claiming incomplete coverage as success.

## Status

The 2026-10-20 baseline recorded 604 diagnostics across the workspace
(core 393, Matrix 30, XMPP 30, Chatmail 72, SimpleX 79). All seven adapter
source trees and the core contracts slice have since migrated; the passing
gate covers **129 source files** (five core slice files plus the LXMF 18,
MeshCore 19, Meshtastic 19, Matrix 28, XMPP 12, Chatmail 13, and SimpleX 15
adapter files). Core's whole tree remains the only outstanding surface: the
`--all` audit reports roughly 400 diagnostics there under the synced dev
environment, including four missing-stub notices for the `yaml` dependency.
A clean gate does not erase those; they are real outstanding work.

## Next work, in risk order

1. **Core storage and pipeline contracts.** The largest groups involve SQLite
   mixin method contracts (`attr-defined`), target-delivery and outbox types,
   event metadata, and replay typing. Introduce narrow `Protocol`s and typed
   payload structures at module boundaries rather than blanket `cast`s.
2. **Optional SDK typing.** Install pinned runtime dependencies in dedicated
   provider CI jobs, add precise stubs or SDK-specific protocols for untyped
   APIs (including `types-PyYAML` for core's `yaml` usage), and remove the
   missing-import exemption when packages/stubs are
   available. Keep adapter discovery SDK-free.
3. **Testing and CI.** Promote passing source slices to the gate after normal
   focused unit tests. Once core's whole tree is clean, make whole-tree
   strict typing mandatory in CI; the pinned mypy tool already ships in the
   locked `dev` extra with `pyproject.toml` and `uv.lock` updated together.

This is not a request to change transport runtime behavior merely to appease a
checker. Guard optional lifecycle state honestly, narrow at validation
boundaries, and protect existing native protocol semantics with tests.
