# Incremental strict typing across MEDRE

This is a **migration**, not a claim that the entire project passes strict mypy.
The codebase supports Python 3.11+, so keep the checker pinned to the lowest
supported syntax/typing semantics while developing on newer interpreters.

## Source and execution model

Restore core and the seven independent adapter distributions as sibling
repositories. From the MEDRE core checkout run:

```console
python -m pip install mypy==2.4.0 msgspec==0.22.0
python scripts/check_strict_typing.py
python scripts/check_strict_typing.py --all
```

The first command is the currently passing gate; `--all` is a deliberately
**failing inventory** until every source tree is migrated. Both run the real
`mypy --strict` checker and analyze imported core symbols for adapters through
`MYPYPATH`, not by replacing MEDRE core with an untyped stub. They use
`--follow-imports=silent` to avoid reporting errors outside the selected
repository. A generated per-module configuration exempts only the optional
transport SDK modules declared in the checker, because those SDKs are not part
of the offline typing toolchain; any other unresolved import — including a
misspelled MEDRE-owned module — fails the gate instead of degrading to `Any`.
Caller-provided `MYPYPATH` entries are kept after the workspace roots, so
external stub paths stay usable without shadowing the trees under check.
The SDK exemptions must be removed or
replaced with precise typed SDK boundaries before claiming full strict typing.
Do **not** silence local error codes globally, use `Any` to bypass a concrete
known type, or add broad per-module ignores to make this migration look complete.

`--workspace` can point to a different directory containing `medre` and the
adapter repos; the default is the core repository's parent. The gate requires
all three migrated adapter repositories to be present, and fails if one is
missing rather than claiming incomplete coverage as success.

## Status at the 2026-10-20 stack

The baseline uses `medre` at `792a5c1a` (`main` in the uploaded stack),
not the parallel `feat/plugin-contract` branch, and the seven adapters at the
commits recorded in that stack's `manifest.tsv`. All figures refer to **source
files**, not tests. The same invocation was used for every repository.

| Repository | Source files | Remaining errors after first increment |
| --- | ---: | ---: |
| MEDRE core (whole tree) | 232 | 393 |
| LXMF adapter | 18 | 0 |
| MeshCore adapter | 19 | 0 |
| Meshtastic adapter | 19 | 0 |
| Matrix adapter | 28 | 30 |
| XMPP adapter | 12 | 30 |
| Chatmail adapter | 13 | 72 |
| SimpleX adapter | 15 | 79 |

The passing core slice is `core/contracts`, `adapter_spec.py`, and
`adapter_registry.py` (five files). Together with the three complete adapters
this gives a strict gate over **61 source files**. The remaining whole-tree
errors are real outstanding work; a clean gate does not erase them.

## Next work, in risk order

1. **Core storage and pipeline contracts.** The largest groups involve SQLite
   mixin method contracts (`attr-defined`), target-delivery and outbox types,
   event metadata, and replay typing. Introduce narrow `Protocol`s and typed
   payload structures at module boundaries rather than blanket `cast`s.
2. **Matrix and XMPP.** Resolve typed dynamic dataclass replacements, nullable
   SDK clients, task result types, and event parsing. These are relatively
   small strict migrations (30 errors each in this snapshot).
3. **Chatmail and SimpleX.** Model the dynamic native SDK/RPC boundary with
   explicit structural protocols and validated decoded payloads. Distinguish
   wire values from trusted canonical values; avoid arbitrary `Any` propagation.
4. **Optional SDK typing.** Install pinned runtime dependencies in dedicated
   provider CI jobs, add precise stubs or SDK-specific protocols for untyped
   APIs, and remove the missing-import exemption when packages/stubs are
   available. Keep adapter discovery SDK-free.
5. **Testing and CI.** Promote passing source slices to the gate after normal
   focused unit tests. Once core and all adapters are clean, make whole-tree
   strict typing mandatory in CI and add the pinned mypy tool to the locked
   development environment with `pyproject.toml` and `uv.lock` updated together.

This is not a request to change transport runtime behavior merely to appease a
checker. Guard optional lifecycle state honestly, narrow at validation
boundaries, and protect existing native protocol semantics with tests.
