# Route Plan Dry-Run Audit

Date: 2026-06-16
Branch: `route-plan-dry-run`

Auditor: static analysis only — no tests executed, no runtime started.

## Summary

MEDRE's runtime already contains a deterministic, side-effect-free route
expansion engine (`src/medre/runtime/route_engine.py`) that knows everything
a `medre routes plan` command would need to display: per-route legs after
directionality expansion, per-channel legs after `channel_room_map`
expansion, origin-label provenance (per-entry → route → adapter), and the
duplicate-room fan-in decision (allowed for Meshtastic→Matrix, rejected for
any leg that creates Matrix→Meshtastic routing). The expansion entry points
(`build_runtime_routes`, `_expand_all_routes`, `check_route_loops`,
`ExpandedRouteProvenance`, `RouteValidationError`) are pure functions over
the parsed `RouteConfigSet` plus an `adapter_id → platform` map, and the
platform map is derivable from config alone via
`config.adapters.all_configs()` returning `(transport, adapter_id, rtc)`
tuples — no adapter is ever started, no SDK is imported, no I/O is
performed. So the entire plan surface is buildable offline.

What exists today is three route subcommands — `medre routes validate`,
`medre routes topology`, `medre routes list` — plus the route inventory in
`medre config check`. All four are config-level summaries: they iterate
`RouteConfig` objects and print one line per declared route. None of them
expands `channel_room_map`, none surfaces the origin-label precedence chain,
none shows the expanded leg count, none explains the fan-in decision, none
calls `check_route_loops`, and none produces JSON. The validate command is
the closest to a plan: it does invoke `build_runtime_routes`
(`src/medre/cli/route_commands.py:87-95`) purely to surface
`RouteValidationError` from the duplicate-room check — but then discards the
returned `list[Route]` and prints the un-expanded `RouteConfig` view instead.

The gap is therefore **operator visibility**, not engine capability. Every
piece of information the spec asks a plan command to display is already
computed by the runtime; it is just not rendered. The reusable surface is
unusually clean: a plan command can be implemented by calling
`_expand_all_routes` (or `build_runtime_routes` + a parallel provenance
walk) and formatting the resulting `list[Route]` plus the
`expanded_route_id → config_route_id` mapping, with optional
`check_route_loops` output and adapter-`origin_label` lookup from
`config.adapters`. No new expansion code is required.

## Methodology

Static read of every file touched by route declaration, expansion,
validation, registration, and CLI rendering. Files read in full:

- `src/medre/runtime/route_engine.py` (1371 lines, complete)
- `src/medre/config/routes.py` (1227 lines, complete)
- `src/medre/config/model.py` (583 lines, complete)
- `src/medre/core/routing/models.py` (162 lines, complete)
- `src/medre/cli/main.py`, `route_commands.py`, `config_commands.py`
  (complete)
- `src/medre/runtime/builder.py` lines 580–840 (route registration and
  Matrix auto-join derivation)
- `src/medre/config/adapters/{matrix,meshtastic,meshcore,lxmf}.py`
  (field surfaces)
- `tests/test_runtime_routing.py`,
  `tests/test_channel_room_map_context_labels.py`,
  `tests/test_channel_room_map_duplicate_room_fanin.py`,
  `tests/test_origin_label_precedence.py`,
  `tests/test_cli_route_workflows.py`,
  `tests/helpers/cli.py`

Git tree on branch `route-plan-dry-run` is clean at commit `1962d5a`; no
prior `routes plan` work is in flight. No tests were executed per task
constraints.

## Findings

### [F-001] `medre routes plan` does not exist

- **Category**: missing operator visibility
- **Location**: `src/medre/cli/main.py:84-95` (the `routes` subparser
  registers only `validate`, `topology`, `list`)
- **Current state**: Three route subcommands are registered:
  `routes validate`, `routes topology`, `routes list`. None is named `plan`
  and none renders the post-expansion view that the tranche spec asks for
  (expanded legs, origin-label provenance, channel_room_map fan-out,
  fan-in decisions). The string `plan` does not appear in any CLI source
  file in a route context.
- **Expected state**: A `medre routes plan [--config PATH] [--json]`
  subcommand that renders the expansion the runtime will perform, without
  any network I/O.
- **Recommendation**: Register a fourth `plan` subcommand in the
  `routes_sub` parser group (`src/medre/cli/main.py:84-95`) and dispatch
  it from the `args.command == "routes"` branch (`src/medre/cli/main.py:467-475`).
  Implementation lives in a new `_routes_plan` helper in
  `src/medre/cli/route_commands.py` and reuses the engine described under
  [Reusable Components](#reusable-components). No engine code changes
  required.

### [F-002] `routes topology` does not expand `channel_room_map`

- **Category**: missing operator visibility
- **Location**: `src/medre/cli/route_commands.py:149-247` (`_routes_topology`)
- **Current state**: The topology preview renders one block per
  `RouteConfig` and prints a single `src_room=` / `src_ch=` /
  `dst_room=` / `dst_ch=` pair (lines 198-207) using an `if … elif …`
  chain. Because `channel_room_map` is mutually exclusive with those four
  fields (`src/medre/config/routes.py:985-999` rejects any combination),
  a route that uses `channel_room_map` prints with **no targeting
  information at all** — the operator sees the route name and the adapter
  arrow but no per-channel leg breakdown. The expanded route IDs
  (`{route_id}__ch{ch}__matrix_to_meshtastic`,
  `{route_id}__ch{ch}__meshtastic_to_matrix`) are never shown.
- **Expected state**: Plan output should show one row per expanded leg
  with the channel→room mapping and the platform direction.
- **Recommendation**: The plan command should call
  `_expand_all_routes(routes, adapter_platforms)` and render each
  `Route` from the returned `list[Route]`. For `channel_room_map` routes,
  each expanded leg's `source.channel` / `targets[0].channel` pair is the
  per-channel mapping — no extra bookkeeping required.

### [F-003] `routes list` omits `channel_room_map` and all origin labels

- **Category**: missing operator visibility
- **Location**: `src/medre/cli/route_commands.py:250-318` (`_routes_list`)
- **Current state**: The most verbose existing renderer still does not
  mention any of: `channel_room_map`, `source_origin_label`,
  `dest_origin_label`, `retry`. The targeting block (lines 283-290) only
  handles `source_room`/`source_channel`/`dest_room`/`dest_channel`, so
  the same invisibility as F-002 applies. Per-entry labels
  (`ChannelRoomMapEntry.source_origin_label`,
  `.dest_origin_label`) are never surfaced even though they are the
  primary precedence override.
- **Expected state**: Plan output should show the origin-label precedence
  chain for every expanded leg — effective label, source of the label
  (per-entry / route-level / adapter fallback / unset), and any explicit
  `""` suppression.
- **Recommendation**: Add a per-leg `origin_label` line to the plan
  renderer and annotate it with its source. The effective label is
  already resolved on `RouteSource.origin_label`
  (`src/medre/runtime/route_engine.py:462-467, 721-725, 743-747`); the
  plan command only needs to walk back from the leg to the
  `ChannelRoomMapEntry` (or the route-level field) to label the source.

### [F-004] `routes validate` discards the expanded route list

- **Category**: missing operator visibility
- **Location**: `src/medre/cli/route_commands.py:87-98`
- **Current state**: validate is the only existing command that calls
  `build_runtime_routes`. It does so purely to catch
  `RouteValidationError` from the duplicate-room fan-in check
  (`src/medre/runtime/route_engine.py:481-556`) — the returned
  `list[Route]` is bound to the throwaway name `_build_runtime_routes(...)`
  and discarded. The route summary printed afterwards (lines 100-117)
  iterates the un-expanded `RouteConfigSet`, not the expansion result.
- **Expected state**: Plan should show both the expansion result and the
  fan-in decision rationale.
- **Recommendation**: The plan command should hold on to the expanded
  list and walk it. When the expansion raises
  `RouteValidationError`, the plan should print the offending route and
  the duplicate-room list — `RouteValidationError` already includes the
  sorted duplicate rooms in its message
  (`src/medre/runtime/route_engine.py:547-556`), so the renderer can
  surface it verbatim or parse it.

### [F-005] Origin-label precedence chain is invisible in every CLI surface

- **Category**: missing operator visibility
- **Location**:
  - Per-entry parse: `src/medre/config/routes.py:520-638`
    (`_parse_channel_room_map_entry`)
  - Route-level parse: `src/medre/config/routes.py:939-973`
  - Expansion resolution: `src/medre/runtime/route_engine.py:680-690,
712-747`
  - Adapter-level fallback: `src/medre/config/adapters/matrix.py:81`,
    `meshtastic.py:122`, `meshcore.py:128`, `lxmf.py:119`
- **Current state**: The full precedence chain
  (`per-entry → route-level → adapter origin_label → ""`) is implemented
  end-to-end and well tested
  (`tests/test_channel_room_map_context_labels.py`,
  `tests/test_origin_label_precedence.py`), but no CLI command displays
  any of: the per-entry labels on a `channel_room_map`, the route-level
  `source_origin_label` / `dest_origin_label`, the adapter
  `origin_label`, or the resolved effective label on an expanded leg. The
  `""`-suppression sentinel (TC-014 in the test file) is invisible to
  operators.
- **Expected state**: Plan output should show the resolved label and its
  origin for every leg.
- **Recommendation**: For each expanded `Route`, look up the originating
  `ChannelRoomMapEntry` via `route_id → config_route_id` provenance and
  the channel parsed from the `__ch{N}__` suffix, then print
  `label=… (source: per-entry|route|adapter|unset|suppressed)`. Adapter
  `origin_label` is one attribute access away via
  `config.adapters.<transport>.<id>.config.origin_label`.

### [F-006] Duplicate-room fan-in decision is not surfaced as a _decision_

- **Category**: missing operator visibility
- **Location**: `src/medre/runtime/route_engine.py:481-556`
  (`_validate_duplicate_rooms_for_direction`)
- **Current state**: The fan-in rule (duplicate Matrix rooms are safe
  only when the route creates no Matrix→Meshtastic leg) is enforced at
  expansion time and raises `RouteValidationError` on violation
  (`tests/test_channel_room_map_duplicate_room_fanin.py:202-275`). When
  the rule **passes** (the allowed fan-in case), nothing in any CLI
  output tells the operator that the configuration was inspected and
  accepted _because_ the inbound channel disambiguates. The operator
  only learns about fan-in when it fails.
- **Expected state**: Plan should annotate fan-in routes with the
  decision: `fan_in=allowed (mesh→matrix only)` or
  `fan_in=blocked (matrix→mesh leg would be ambiguous)`.
- **Recommendation**: The plan renderer can detect the fan-in shape
  directly (duplicate `targets[0].channel` across legs whose
  `source.adapter` is the Meshtastic side) and print an annotation. No
  engine change needed — the decision is implied by the legs that
  `_expand_channel_room_map_route` produces.

### [F-007] `check_route_loops` is never invoked from any CLI command

- **Category**: missing operator visibility
- **Location**: `src/medre/runtime/route_engine.py:868-949`
  (`check_route_loops`)
- **Current state**: Loop detection (fast-path A↔B and slow-path DFS over
  the adapter adjacency graph) runs only inside `register_routes`
  (`src/medre/runtime/route_engine.py:1133`), which is called by the
  runtime builder (`src/medre/runtime/builder.py:638`). The CLI route
  commands never call it. Bidirectional bridges therefore produce
  intentional loops that the operator never sees previewed.
- **Expected state**: Plan should show detected cycles as informational
  notes (loops do not block registration per
  `src/medre/runtime/route_engine.py:877-879`).
- **Recommendation**: Plan should call
  `check_route_loops(expanded_routes)` and print each returned message
  under a `Topology notes:` section. Reuse is one function call.

### [F-008] `ExpandedRouteProvenance` dataclass is declared but unused

- **Category**: stale assumptions
- **Location**: `src/medre/runtime/route_engine.py:167-189`
- **Current state**: The frozen dataclass
  `ExpandedRouteProvenance(config_route_id, expanded_route_id, route)` is
  exported in `__all__` (line 47) and documented as the
  "`(config_route_id, expanded_route_id, Route)` triple … so that
  downstream consumers can deterministically map any expanded route back
  to the configuration entry." In practice `_expand_all_routes` returns
  a plain `dict[str, str]` (`expanded_id → config_id`), not a list of
  `ExpandedRouteProvenance`. The class is dead code at the engine layer;
  the `RouteRegistrationResult.provenance` field is typed as
  `dict[str, str]` (line 277).
- **Expected state**: The plan command is the natural consumer the class
  was forward-declared for.
- **Recommendation**: Either (a) have `_expand_all_routes` return
  `list[ExpandedRouteProvenance]` and derive the dict from it, or (b)
  leave the dict as-is and let the plan command build the triples
  locally by zipping the dict with the routes list. Option (b) is the
  smaller diff (no engine change) and matches the ponytail principle;
  option (a) is the cleaner long-term shape if the class is to earn its
  keep. Either is acceptable.

### [F-009] Adapter `origin_label`, `relay_prefix`, and platform hint not in route previews

- **Category**: missing operator visibility
- **Location**: `src/medre/config/adapters/matrix.py:81-82`,
  `meshtastic.py:122,128`, `meshcore.py:128,135`, `lxmf.py:119,126`
- **Current state**: `_routes_topology` prints `adapter_id(transport)`
  for each source/dest adapter (`src/medre/cli/route_commands.py:176-187`)
  but never the adapter's own `origin_label` or `relay_prefix`, so the
  operator cannot see what the renderer would fall back to when a route
  sets no route-level or per-entry label. The transport string is shown,
  but the adapter_kind (`real` vs `fake`) is not, even though it is
  available on every `RuntimeConfig` wrapper (`adapter_kind: str = "real"`
  in `MatrixRuntimeConfig`, etc.).
- **Expected state**: Plan should show adapter origin_label and
  adapter_kind alongside the transport so the precedence chain is
  traceable end-to-end.
- **Recommendation**: Add a small adapter inventory section at the top
  of the plan output (or per-leg inline): `adapter_id  transport=…
kind=…  origin_label=…  relay_prefix=…`. All fields are read-only on
  the frozen config.

### [F-010] No `--json` output on any route subcommand

- **Category**: missing operator visibility
- **Location**: `src/medre/cli/main.py:84-95` (no `--json` registered on
  `routes validate|topology|list`)
- **Current state**: `smoke`, `evidence`, `inspect`, `trace`, `replay`,
  `recover` all accept `--json` and emit normalized JSON reports
  (`docs/dev/testing.md:274-289` documents the convention via
  `assert_report_shape`). The three route commands predate the JSON
  convention and are print-only. Operators scripting CI cannot
  programmatically inspect route expansion.
- **Expected state**: Plan should emit a normalized JSON report for CI
  consumption.
- **Recommendation**: Add `--json` to the `plan` subparser from day one
  (do not retrofit the older three commands in this tranche). The JSON
  shape should mirror `RouteRegistrationResult`: `{routes: […],
provenance: {…}, eligibility_summary: {…}, loops: […]}`. The
  `tests/helpers/assertions.py::assert_report_shape` helper can be
  reused for the test suite.

### [F-011] Expanded route ID suffix scheme is undocumented in any operator surface

- **Category**: unclear docs
- **Location**: `src/medre/runtime/route_engine.py:449-476, 711, 735`
- **Current state**: The expansion produces three deterministic ID
  shapes:
  - `{route_id}__{N}` — multi-source forward expansion (line 458)
  - `{route_id}__rev_{N}` — reverse leg of a bidirectional route
    (line 456)
  - `{route_id}__ch{channel}__matrix_to_meshtastic` /
    `{route_id}__ch{channel}__meshtastic_to_matrix` — per-channel legs
    (lines 711, 735)
    The collision-guard at lines 805-814 enforces uniqueness and rejects
    user route IDs that clash with the pattern. None of this is visible to
    operators: the existing commands print only the original `route_id`,
    never the expanded IDs. An operator who writes
    `replay --route-ids bridge__ch0__meshtastic_to_matrix` (a valid
    expanded ID per `src/medre/cli/main.py:325-331`) has no way to
    discover the ID without reading source.
- **Expected state**: Plan output should print the expanded IDs and the
  suffix scheme should be documented in `docs/ops/configuration.md` or
  the routes section of the operator workflow.
- **Recommendation**: Plan should print one row per expanded `Route`
  with its full `route.id`, and a short footnote describing the three
  suffix patterns. Out of scope for the code diff; in scope for the
  audit call-out.

### [F-012] Bidirectional routes print as one line but expand to two

- **Category**: missing operator visibility
- **Location**: `src/medre/cli/route_commands.py:171-212` (topology) and
  `250-318` (list); expansion at
  `src/medre/runtime/route_engine.py:801-803`
- **Current state**: A `bidirectional` route produces a forward leg
  (`{route_id}` for single-source or `{route_id}__{N}`) plus a reverse
  leg (`{route_id}__rev_{N}`). Both `topology` and `list` render the
  `RouteConfig` once with a `<->` arrow, so the operator cannot see the
  actual two-route shape the router will receive. For a multi-source
  bidirectional route the gap is worse: `N` forward routes + 1 reverse
  route with `N` targets all collapse to one printed block.
- **Expected state**: Plan should show the leg count and each leg.
- **Recommendation**: Plan must iterate the expanded list, not the
  config-level list. This is the central design decision for the
  command.

### [F-013] Matrix auto-join room derivation is invisible pre-flight

- **Category**: missing operator visibility
- **Location**: `src/medre/runtime/builder.py:780-839`
  (`_derive_matrix_auto_join_rooms`)
- **Current state**: The runtime builder walks the expanded routes to
  compute the set of Matrix rooms each Matrix adapter must auto-join,
  and validates that any explicit `room_allowlist` on a Matrix config
  includes every route-derived source room. This derivation runs only at
  runtime build time; the existing CLI commands do not preview it. An
  operator can run `medre config check` and see "valid", then hit a
  `RuntimeConfigError` at startup because their `room_allowlist` omits a
  route-derived room.
- **Expected state**: Plan should show the derived auto-join set per
  Matrix adapter and flag `room_allowlist` conflicts before runtime
  start.
- **Recommendation**: The plan command can inline the same derivation
  (it is pure over the expanded routes). If a smaller scope is wanted,
  defer this to a follow-on tranche and document it under
  [Intentionally Deferred](#intentionally-deferred).

### [F-014] No "fan-in" annotation in any preview surface

- **Category**: missing operator visibility
- **Location**: n/a — feature absent
- **Current state**: See F-006. Fan-in is the _allowed_ counterpart of
  the duplicate-room rejection. Today it is implicit; the operator has
  no way to ask "does this route fan in?" without reasoning from the
  expansion themselves.
- **Expected state**: Plan output should label fan-in legs.
- **Recommendation**: Trivial once F-002/F-004 are addressed — the
  annotation is a function of the expanded leg set.

### [F-015] `_routes_validate` recomputes the adapter-id set instead of using `all_configs()`

- **Category**: duplicate/overlapping CLI behavior
- **Location**: `src/medre/cli/route_commands.py:30-33, 90-92`
- **Current state**: validate builds `known_adapter_ids` and
  `adapter_platforms` in two separate loops over
  `config.adapters.all_configs()`. `config_commands._config_check` does
  its own third loop at `src/medre/cli/config_commands.py:119-132`. All
  three compute the same `(adapter_id, transport)` projection. The
  duplication is mild but a `plan` command would add a fourth consumer.
- **Expected state**: Plan should not add a fourth copy of the loop.
- **Recommendation**: Extract a tiny helper
  `def _adapter_id_to_transport(config) -> dict[str, str]` (one liner
  over `all_configs()`) into `route_commands.py` and let plan, validate,
  and the F-016 check in `config_commands.py` all call it. Optional
  cleanup; not required for the tranche.

### [F-016] Disabled-route handling differs across the three commands

- **Category**: duplicate/overlapping CLI behavior
- **Location**:
  - `validate`: skips unknown-ref validation for disabled routes
    (`route_commands.py:46-54`) but still prints them with an `[OFF]`
    marker; warnings about "no enabled source/dest" are gated on
    `route.enabled` (lines 68-79).
  - `topology`: prints `[OFF]` and counts disabled separately
    (lines 209, 245-247).
  - `list`: prints disabled routes with no marker.
  - `register_routes`: silently skips disabled routes at expansion
    (`route_engine.py:787-789`).
- **Current state**: The three commands have three slightly different
  disabled-route policies. `register_routes` excludes them from the
  expansion entirely, so a plan renderer iterating the expansion would
  naturally not show disabled routes — diverging from validate and
  topology which do show them.
- **Expected state**: Plan should show disabled routes (operators want
  to see what would activate when they flip `enabled: true`) but clearly
  mark them as inactive.
- **Recommendation**: Plan should iterate `route_config_set.routes`
  (config-level, includes disabled) and annotate each with its expansion
  outcome — "disabled", "N legs", "blocked: duplicate-room ambiguous",
  etc. Use the provenance dict to map expanded IDs back. The renderer
  must not rely solely on the expansion result, because disabled routes
  are absent from it.

## Existing Route Expansion Architecture

This section documents how `src/medre/runtime/route_engine.py` turns a
`RouteConfigSet` into the list of `Route` objects a `medre routes plan`
command would display.

### Public entry points

```python
def build_runtime_routes(
    route_config_set: RouteConfigSet,
    adapter_platforms: dict[str, str] | None = None,
) -> list[Route]: ...
```

Top-level convenience. Returns only the expanded routes — discards the
provenance dict. Defined at `src/medre/runtime/route_engine.py:822-860`.

```python
def _expand_all_routes(
    route_config_set: RouteConfigSet,
    adapter_platforms: dict[str, str] | None = None,
) -> tuple[list[Route], dict[str, str]]:
```

Private but more useful for plan: returns `(routes, provenance)` where
`provenance` maps `expanded_route_id → config_route_id`. This is the
function the plan command should call. Defined at lines 758-819.

```python
def register_routes(
    router, route_config_set, adapter_ids, built_adapter_ids=None,
    adapter_platforms=None,
) -> RouteRegistrationResult
```

Full registration. Calls `_expand_all_routes`, then degrades routes
whose adapters failed to build, runs `check_route_loops`, and registers
on the `Router`. Plan should **not** call this — it requires a live
`Router` and performs degradation that requires build-time adapter
state, which is out of scope for an offline plan.

### Per-route expansion decision tree

`_expand_all_routes` iterates `route_config_set.routes` in order. For
each `RouteConfig`:

1. **Skip disabled** (`if not rc.enabled: continue`, line 787). Disabled
   routes are absent from the returned list and the provenance dict.
2. **Dispatch on `channel_room_map`** (line 794):
   - If `rc.channel_room_map is not None` → call
     `_expand_channel_room_map_route(rc, adapter_platforms)` (line 796).
   - Else dispatch on `directionality` (lines 797-803):
     - `SOURCE_TO_DEST` → `_expand_route_config(rc)`
     - `DEST_TO_SOURCE` → `_expand_route_config(rc, swap_direction=True)`
     - `BIDIRECTIONAL` → both
3. **Uniqueness guard** (lines 805-814): each expanded ID is checked
   against the provenance dict; collision raises
   `RouteValidationError` with the pattern description.

### `_expand_route_config` — non-`channel_room_map` path

Defined at lines 389-478. Produces one `Route` per source adapter:

- Single source, no swap → `route_id = rc.route_id` (line 454).
- Multi-source forward → `route_id = f"{rc.route_id}__{src_idx}"` (line 458).
- Any swap (reverse leg) → `route_id = f"{rc.route_id}__rev_{src_idx}"`
  (line 456).

Each route gets all `dest_adapters` as `RouteTarget` entries (line 460).
`source_channel` / `dest_channel` come from the (possibly swapped)
`rc.source_channel` / `rc.dest_channel`. `origin_label` is taken from
`rc.source_origin_label` (forward) or `rc.dest_origin_label` (reverse)
at lines 429 / 435.

### `_expand_channel_room_map_route` — `channel_room_map` path

Defined at lines 559-755. This is the most complex expansion and the
one the existing CLI surfaces render least well.

1. **Adapter-platform resolution** (lines 602-628): requires exactly one
   source and one dest adapter; looks up both in `adapter_platforms`;
   requires one Matrix and one Meshtastic. Failure modes:
   - Wrong adapter count → `RouteValidationError` (line 596).
   - Missing platform → `RouteValidationError` (lines 609-618).
   - Wrong platform pair → `RouteValidationError` (lines 623-628).
2. **Direction identification** (lines 630-645): computes
   `fwd_is_matrix_to_mesh` — `True` when source is Matrix, `False`
   when source is Meshtastic. This boolean controls which
   directionality flags produce which physical leg.
3. **Route-level duplicate-room check** (lines 651-653 →
   `_validate_duplicate_rooms_for_direction` at 481-556): scans
   `channel_room_map.values()` for duplicate `room` fields. If duplicates
   exist, decides whether they are safe based on whether the route
   creates a Matrix→Meshtastic leg (ambiguous) or only Meshtastic→Matrix
   (safe fan-in). Raises `RouteValidationError` listing the sorted
   duplicate rooms on the ambiguous path (lines 547-556).
4. **Per-channel loop** (lines 668-754): iterates
   `sorted(rc.channel_room_map.items())`. For each channel:
   - Extract `(room_id, entry_source_label, entry_dest_label)` from
     `ChannelRoomMapEntry` or falls back to bare-string handling
     (lines 671-678).
   - Resolves effective per-entry labels with the `is not None` check
     so that explicit `""` is preserved as suppression and `None` falls
     through to the route-level label (lines 680-690).
   - Computes which legs to create based on `directionality` and
     `fwd_is_matrix_to_mesh` (lines 692-707).
   - Emits one or two `Route` objects per channel with deterministic IDs
     `{route_id}__ch{channel}__matrix_to_meshtastic` (line 711) and/or
     `{route_id}__ch{channel}__meshtastic_to_matrix` (line 735).
   - Forward leg uses the effective source-side label; reverse leg uses
     the effective dest-side label (lines 712-747).

### Origin-label precedence chain (implemented)

The chain is split across two layers:

1. **At expansion time** (`route_engine.py:680-690, 712-747`):
   - per-entry label wins if it is not `None` (so explicit `""`
     suppresses);
   - else route-level `source_origin_label` / `dest_origin_label` is
     used;
   - else the field stays `None` on `RouteSource.origin_label`.
2. **At render time** (covered by
   `tests/test_origin_label_precedence.py` and
   `src/medre/core/rendering/attribution.py`):
   - if `RenderingContext.source_origin_label` is not `None` (including
     `""`), it wins;
   - else the source adapter's `origin_label` is used;
   - else the rendered value is empty.

So the full chain is: **per-entry → route-level → adapter → ""**. The
expansion layer resolves the first two steps; the rendering layer
resolves the last two. A plan command can show the first two directly
plus the adapter fallback (third step) by reading
`config.adapters.<transport>.<id>.config.origin_label`. The fourth step
(empty) is the default.

### Duplicate-room fan-in rule (implemented)

The route-level check at `_validate_duplicate_rooms_for_direction`
(lines 481-556) is the single source of truth. It runs once per
`channel_room_map` route, after platform assignment but before the
per-channel loop. Rule:

- Duplicate `room` values across the map → collect into `dupes` set.
- `dupes` empty → always safe, return.
- Compute `create_matrix_to_mesh` from `rc.directionality` and
  `fwd_is_matrix_to_mesh` (lines 532-544).
- If the route creates any Matrix→Meshtastic leg and `dupes` is
  non-empty → raise `RouteValidationError` listing the duplicate rooms.

The rule is directional: a bidirectional route with duplicate rooms is
always rejected because it always creates a Matrix→Meshtastic leg. A
`source_to_dest` Meshtastic→Matrix route with duplicate rooms is always
allowed (fan-in). A `dest_to_source` Matrix-source route with duplicate
rooms is allowed because the reverse leg is Meshtastic→Matrix. All four
combinations are tested in
`tests/test_channel_room_map_duplicate_room_fanin.py:109-275`.

### Loop detection (implemented, unused by CLI)

`check_route_loops(routes)` at lines 868-949 takes the expanded
`list[Route]` and returns `list[str]` of human-readable cycle
descriptions. Two layers:

1. **Fast path** (lines 904-915): direct A↔B pair detection.
2. **Slow path** (lines 917-948): DFS over the adapter adjacency graph
   for multi-hop cycles (X→Y→Z→X). Disabled routes are excluded because
   the function checks `route.enabled` (line 890).

Both layers return descriptive strings but do **not** block
registration (documented at lines 877-879). The runtime logs them at
`DEBUG` (line 1135). Plan output should log them at `INFO` or print
under a notes section.

### What can be observed without runtime state

The entire expansion pipeline is observable offline given:

- A parsed `RouteConfigSet` (from `medre.config.loader.load_config`).
- An `adapter_id → platform` mapping, derivable from
  `config.adapters.all_configs()` which returns
  `(transport, adapter_id, rtc)` tuples (`src/medre/config/model.py:512-523`).

What is **not** observable offline:

- **Build degradation**: which routes get degraded or skipped because
  an adapter failed to build. This requires
  `built_adapter_ids` which only exists after `RuntimeBuilder._build_adapters`
  runs (`src/medre/runtime/builder.py:618`).
- **Startup readiness**: `compute_startup_readiness` requires
  per-adapter lifecycle states from `MedreApp.start()`.
- **Matrix auto-join derivation**: this _is_ computable offline (the
  builder's `_derive_matrix_auto_join_rooms` only calls
  `build_runtime_routes` plus adapter config inspection), but the
  existing `room_allowlist` validation happens inside the builder, not
  the engine — see F-013.

## Reusable Components

The plan command can reuse the following without modification:

| Component                                                                     | Location                                               | Purpose in plan                                                    |
| ----------------------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------ |
| `build_runtime_routes(rcs, adapter_platforms)`                                | `route_engine.py:822-860`                              | Produce `list[Route]`                                              |
| `_expand_all_routes(rcs, adapter_platforms)`                                  | `route_engine.py:758-819`                              | Produce `(routes, provenance)` — preferred for plan                |
| `ExpandedRouteProvenance` dataclass                                           | `route_engine.py:167-189`                              | Optional structured triple (see F-008)                             |
| `check_route_loops(routes)`                                                   | `route_engine.py:868-949`                              | Cycle notes section                                                |
| `RouteValidationError`                                                        | `route_engine.py:293-294`                              | Catch and render fan-in / collision errors                         |
| `validate_route_adapter_refs`                                                 | `route_engine.py:302-346`                              | Pre-flight unknown-ref check (already used by `_routes_validate`)  |
| `RouteConfigSet`, `RouteConfig`, `ChannelRoomMapEntry`, `RouteDirectionality` | `config/routes.py:39, 738, 388`                        | Config-level objects                                               |
| `Route`, `RouteSource`, `RouteTarget`                                         | `core/routing/models.py:127, 29, 98`                   | Expanded route objects (frozen, serializable)                      |
| `config.adapters.all_configs()`                                               | `config/model.py:512-523`                              | Build the `adapter_platforms` map                                  |
| `config.adapters.all_enabled()`                                               | `config/model.py:503-510`                              | Compute the configured-enabled set for ref validation              |
| Adapter `origin_label` / `relay_prefix`                                       | `config/adapters/{matrix,meshtastic,meshcore,lxmf}.py` | Render the precedence chain's adapter fallback                     |
| `medre.config.loader.load_config`                                             | `config/loader.py`                                     | Standard config load (already used by the three existing commands) |
| `tests/helpers/cli.py::_run_cli`                                              | `tests/helpers/cli.py:328-339`                         | CLI test runner that captures stdout                               |
| `tests/helpers/assertions.py::assert_report_shape`                            | per `docs/dev/testing.md:274-289`                      | JSON shape assertion for the `--json` output                       |

The plan command needs to add:

- A renderer that walks the expanded `list[Route]` and prints one row
  per leg.
- A renderer for the JSON shape.
- An optional adapter inventory section (F-009) and a loops notes
  section (F-007).
- A fan-in annotation (F-006, F-014).

No engine changes are required.

## CLI Surface Inventory

| Command                                               | What it does                                                                                                                                                                                                                                                        | Reusable for plan?                                                                                                                                                   |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `medre routes validate` (`route_commands.py:13-147`)  | Loads config, prints per-`RouteConfig` block, calls `build_runtime_routes` only to catch `RouteValidationError`, then discards the result. Prints warnings for routes whose source/dest adapters are all disabled. Exits `EXIT_CONFIG` on errors.                   | Partial — the `load_config` + `adapter_platforms` build (lines 90-92) and the unknown-ref check pattern are directly reusable. The renderer is not.                  |
| `medre routes topology` (`route_commands.py:149-247`) | Loads config, prints one block per `RouteConfig` with adapter(transport) labels, direction arrow, source/dest targeting fields (but **not** `channel_room_map`), optional policy summary, and an `N/M route(s) active` count. Does not call `build_runtime_routes`. | Partial — the adapter(transport) label format is reusable. The targeting block (lines 198-207) must be replaced because it silently drops `channel_room_map`.        |
| `medre routes list` (`route_commands.py:250-318`)     | Loads config, prints the most verbose per-`RouteConfig` block: status, direction, source/dest adapter tuples, targeting fields (same gap as topology), filter_hooks, and a policy subsection. Does not call `build_runtime_routes`.                                 | Partial — the field-by-field format is a useful template for the per-leg renderer. Must add `channel_room_map`, `source_origin_label`, `dest_origin_label`, `retry`. |
| `medre config check` (`config_commands.py:55-232`)    | Loads config, prints adapter inventory, runs the F-016 unknown-ref check inline (lines 119-132), prints a route inventory (lines 178-201) using the same one-line-per-`RouteConfig` format as validate, and prints a startup preview (lines 216-232).               | Partial — the F-016 check is reusable and should be deduplicated (F-015). The startup preview is the closest existing artifact to a plan but is route-ID-only.       |
| `medre routes plan`                                   | **Does not exist.**                                                                                                                                                                                                                                                 | n/a — new command.                                                                                                                                                   |

## Coverage Matrix

| Audit area                                     | Status       | Notes                                                                             |
| ---------------------------------------------- | ------------ | --------------------------------------------------------------------------------- |
| `medre routes plan` exists                     | ❌ Missing   | F-001                                                                             |
| Expanded route legs rendered                   | ❌ Missing   | F-002, F-012                                                                      |
| `channel_room_map` per-channel expansion shown | ❌ Missing   | F-002                                                                             |
| Per-entry origin labels shown                  | ❌ Missing   | F-003, F-005                                                                      |
| Route-level origin labels shown                | ❌ Missing   | F-003, F-005                                                                      |
| Adapter `origin_label` shown                   | ❌ Missing   | F-005, F-009                                                                      |
| Explicit `""` suppression visible              | ❌ Missing   | F-005                                                                             |
| Duplicate-room fan-in decision surfaced        | ⚠️ Partial   | F-006, F-014 — failure path shown by validate, allowed path silent                |
| Loop detection output                          | ❌ Missing   | F-007                                                                             |
| `ExpandedRouteProvenance` reused               | ❌ Dead code | F-008                                                                             |
| Adapter platform/transport hint                | ✅ Present   | topology prints `adapter(transport)`                                              |
| Adapter `adapter_kind` (real/fake) shown       | ❌ Missing   | F-009                                                                             |
| Expanded route IDs documented                  | ❌ Missing   | F-011                                                                             |
| `--json` output                                | ❌ Missing   | F-010                                                                             |
| Disabled-route handling consistent             | ⚠️ Partial   | F-016 — three commands, three policies                                            |
| Unknown-ref validation                         | ✅ Present   | F-016 in `config check`; `_routes_validate`                                       |
| Matrix auto-join derivation preview            | ❌ Missing   | F-013                                                                             |
| Reusable expansion entry point                 | ✅ Present   | `build_runtime_routes`, `_expand_all_routes`                                      |
| Reusable loop detection                        | ✅ Present   | `check_route_loops`                                                               |
| Reusable validation                            | ✅ Present   | `validate_route_adapter_refs`                                                     |
| Plan can run without adapter start             | ✅ Yes       | expansion is pure; platform map from config                                       |
| Plan can run without SDK imports               | ✅ Yes       | `_routes_validate` already imports `route_engine` lazily and never imports an SDK |
| Test patterns to follow                        | ✅ Present   | `tests/test_cli_route_workflows.py` + `tests/helpers/cli.py::_run_cli`            |

## Intentionally Deferred

The following are out of scope for the `routes plan` tranche and should
not be added in the same change. They are listed here so the plan
command does not accidentally absorb them.

- **Build degradation preview**. Surfaces routes that would be degraded
  or skipped because an adapter failed to _build_. Requires running
  `RuntimeBuilder._build_adapters` up to the point of failure, which is
  not side-effect-free (it constructs adapter instances even if it does
  not start them). Out of scope for an offline plan; belongs in a future
  `medre runtime preflight` or extends `medre diagnostics`.
- **Startup readiness preview**. `compute_startup_readiness`
  (`route_engine.py:1198-1371`) requires per-adapter lifecycle states
  that only exist after `MedreApp.start()`. Not offline-computable.
- **Live route eligibility**. The runtime mutates `Route.enabled` at
  delivery time (see `core/routing/models.py:126` — `Route` is the one
  non-frozen dataclass). Plan is a config-time view and must not reflect
  runtime mutations.
- **Policy evaluation preview**. `BridgePolicy` allowlists
  (`config/routes.py:78-107`) are evaluated per-delivery by the
  route-policy evaluator, not at expansion time. Plan can show the
  policy _fields_ (already done by `routes list`) but not their effect
  on a hypothetical event.
- **Rendering preview**. Plan shows the route shape, not the rendered
  output. Rendering requires a `RenderingContext` and a target adapter,
  which is delivery-time state.
- **Retrofitting `--json` onto the three existing commands**. F-010
  applies to the new `plan` command only. The older commands predate
  the JSON convention and changing their output shape is a separate
  compatibility decision.
- **`docs/ops/configuration.md` update for the suffix scheme**. F-011
  calls it out; the doc update can ship with or after the plan command
  but is not blocking.
