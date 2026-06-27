# Source-Context Origin Label Audit

Factual audit of the current MEDRE route-config model, route expansion,
and mapped-route (`channel_room_map`) label flow. No aspirational
language; describes running code on branch
`route-context-origin-labels`. This audit is the foundation for the
upcoming source-context origin-label feature work.

All file:line citations are relative to the repository root.

---

## 1. Summary

The current code base already threads a single route-level
`source_origin_label` / `dest_origin_label` pair from
:class:`~medre.config.routes.RouteConfig` through route expansion into
:class:`~medre.core.routing.models.RouteSource.origin_label`, through
:func:`~medre.core.engine.pipeline.target_delivery.TargetDeliveryService.deliver_to_target`
into :class:`~medre.core.rendering.renderer.RenderingContext.source_origin_label`,
and finally into each renderer's precedence resolution (route/context
label > source-attribution registry > empty string). Direction-aware
assignment (forward leg → `source_origin_label`, reverse leg →
`dest_origin_label`) is already implemented for both standard expansion
and `channel_room_map` expansion.

The gap the upcoming feature work must close is **per-entry** label
support for `channel_room_map`. Today the config model parses
`channel_room_map` as a flat `dict[str, str]` mapping Meshtastic
channel index ("0"–"7") to canonical Matrix room ID
(`src/medre/config/routes.py:429`, `src/medre/config/routes.py:597-721`).
There is no slot for per-entry labels, so every expanded leg produced
from one `channel_room_map` inherits the same route-level
`source_origin_label` / `dest_origin_label`. This is explicitly noted
as a known gap in `docs/spec/routing-delivery.md:1366-1371` and
`docs/dev/relay-prefix-attribution-audit.md:709-715`.

The feature must extend the `channel_room_map` value shape to carry
optional per-entry labels, thread those labels through
:func:`~medre.runtime.route_engine._expand_channel_room_map_route`
onto the correct per-leg `RouteSource.origin_label`, and leave the
downstream RenderingContext / renderer precedence chain untouched. No
renderer, formatter, or `RenderingPipeline.render` signature change is
required — the existing `source_origin_label` plumbing already carries
the resolved value end-to-end.

---

## 2. Current route config model

### 2.1 `RouteConfig` dataclass

Defined at `src/medre/config/routes.py:363-431`. The class is
`@dataclass(frozen=True)`; construction is performed by
`RouteConfig.from_dict(route_id, data)` at
`src/medre/config/routes.py:433-798`.

The EXACT current YAML field names (parsed from the route table
dict) are:

| Field                 | Type                             | Default             | Validation site (file:line)                                                                        |
| --------------------- | -------------------------------- | ------------------- | -------------------------------------------------------------------------------------------------- |
| `source_adapters`     | `tuple[str, ...]` (list in YAML) | required, non-empty | `src/medre/config/routes.py:456-472`                                                               |
| `dest_adapters`       | `tuple[str, ...]` (list in YAML) | required, non-empty | `src/medre/config/routes.py:475-491`                                                               |
| `directionality`      | `RouteDirectionality` enum       | `source_to_dest`    | `src/medre/config/routes.py:493-503`                                                               |
| `enabled`             | `bool`                           | `True`              | `src/medre/config/routes.py:506`                                                                   |
| `filter_hooks`        | `tuple[str, ...]`                | `()`                | `src/medre/config/routes.py:509-521` (non-empty is **rejected** — reserved)                        |
| `source_channel`      | `str \| None`                    | `None`              | `src/medre/config/routes.py:524`; aliasing at `src/medre/config/routes.py:531-559`                 |
| `dest_channel`        | `str \| None`                    | `None`              | `src/medre/config/routes.py:525`; aliasing at `src/medre/config/routes.py:543-559`                 |
| `source_room`         | `str \| None`                    | `None`              | `src/medre/config/routes.py:526`; aliases `source_channel` at `src/medre/config/routes.py:556-557` |
| `dest_room`           | `str \| None`                    | `None`              | `src/medre/config/routes.py:527`; aliases `dest_channel` at `src/medre/config/routes.py:558-559`   |
| `policy`              | `BridgePolicy \| None`           | `None`              | `src/medre/config/routes.py:723-741`                                                               |
| `retry`               | `RouteRetryConfig \| None`       | `None`              | `src/medre/config/routes.py:743-756`                                                               |
| `channel_room_map`    | `dict[str, str] \| None`         | `None`              | `src/medre/config/routes.py:597-721`                                                               |
| `source_origin_label` | `str \| None`                    | `None`              | `src/medre/config/routes.py:561-577`                                                               |
| `dest_origin_label`   | `str \| None`                    | `None`              | `src/medre/config/routes.py:579-595`                                                               |

The runtime is YAML-only for config files
(`docs/ops/configuration.md`, AGENTS.md). The historical `from_toml_dict`
constructor name was renamed to `from_dict` when the project dropped TOML
config support; field names and dict shapes are unchanged and the loader
produces plain Python dicts.

### 2.2 `directionality` enum

`RouteDirectionality` is defined at `src/medre/config/routes.py:39-48`:

```python
class RouteDirectionality(Enum):
    SOURCE_TO_DEST = "source_to_dest"
    DEST_TO_SOURCE = "dest_to_source"
    BIDIRECTIONAL = "bidirectional"
```

The YAML key is `directionality` (not `direction`). Unknown values
raise `ConfigValidationError` at `src/medre/config/routes.py:497-503`.

### 2.3 `source_origin_label` / `dest_origin_label` parsing

Both fields are declared on `RouteConfig` at
`src/medre/config/routes.py:430-431`:

```python
source_origin_label: str | None = None
dest_origin_label:   str | None = None
```

Parsing rules (identical for both fields):

1. Absent → `None` (means "unset" — renderers fall back to the source
   adapter's `origin_label` via the source-attribution registry).
2. Present and a `str` → the string value is stored verbatim.
3. Present and a `bool` → `ConfigValidationError`
   (`src/medre/config/routes.py:565-570` for source,
   `src/medre/config/routes.py:583-588` for dest). The bool guard
   runs **before** the generic `isinstance(str)` guard because in
   Python `isinstance(True, str)` is False but `isinstance(True, int)`
   is True; the explicit bool check avoids a confusing type-name in
   the error.
4. Present and any other type → `ConfigValidationError`
   (`src/medre/config/routes.py:571-576` for source,
   `src/medre/config/routes.py:589-594` for dest).

An explicit empty string `""` is accepted and preserved verbatim. This
is the sentinel that suppresses the adapter-level fallback in
renderers (see §4).

### 2.4 `channel_room_map` shape today

Declared at `src/medre/config/routes.py:429`:

```python
channel_room_map: dict[str, str] | None = None
```

Parsed at `src/medre/config/routes.py:597-721`. The shape is a flat
dict mapping a Meshtastic channel index (string "0"–"7", after
normalisation) to a canonical Matrix room ID (string starting with
`"!"`). There is **no per-entry label slot** — values are bare room-ID
strings, not tables.

Normalization and validation steps performed in `from_dict`:

| Step                                                                            | file:line                                                                       |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Reject non-dict                                                                 | `src/medre/config/routes.py:601-606`                                            |
| Mutual exclusion with `source_channel`/`dest_channel`/`source_room`/`dest_room` | `src/medre/config/routes.py:607-621`                                            |
| Require exactly one source adapter                                              | `src/medre/config/routes.py:622-628`                                            |
| Require exactly one dest adapter                                                | `src/medre/config/routes.py:629-634`                                            |
| Reject bool channel key                                                         | `src/medre/config/routes.py:641-646`                                            |
| Accept int or str channel key, normalise to `str(int)`                          | `src/medre/config/routes.py:647-656`                                            |
| Reject non-integer-string channel key                                           | `src/medre/config/routes.py:658-665`                                            |
| Reject channel out of range 0–7                                                 | `src/medre/config/routes.py:666-671`                                            |
| Reject duplicate channel (after normalisation)                                  | `src/medre/config/routes.py:673-678`                                            |
| Reject blank / empty room value                                                 | `src/medre/config/routes.py:682-688`                                            |
| Reject room alias (starting with `#`)                                           | `src/medre/config/routes.py:690-697`                                            |
| Reject non-canonical room ID (must start with `!`)                              | `src/medre/config/routes.py:698-704`                                            |
| Duplicate-room ambiguity (runtime, not config parse)                            | `src/medre/runtime/route_engine.py` (`_validate_duplicate_rooms_for_direction`) |
| Reject empty map (after normalisation)                                          | `src/medre/config/routes.py:716-721`                                            |

The mutual-exclusion check at `src/medre/config/routes.py:607-621` is
important for the feature: per-entry labels have to remain compatible
with this rule. `source_channel` / `dest_channel` are still produced
_per expanded leg_ by the expansion function, not by the config
parser.

### 2.5 Other route validation

Beyond `channel_room_map`, the parser performs these cross-field
checks:

| Check                                                                | file:line                                                           |
| -------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Route ID non-empty                                                   | `src/medre/config/routes.py:60-64`                                  |
| Route ID matches `^[A-Za-z0-9_-]+$`                                  | `src/medre/config/routes.py:55`, `src/medre/config/routes.py:65-70` |
| `source_adapters` / `dest_adapters` must not overlap (no self-route) | `src/medre/config/routes.py:758-768`                                |
| No duplicate entries in `dest_adapters`                              | `src/medre/config/routes.py:771-775`                                |
| No duplicate entries in `source_adapters`                            | `src/medre/config/routes.py:776-780`                                |
| Duplicate `route_id` across the route set                            | `src/medre/config/routes.py:835-843` (`RouteConfigSet.validate`)    |

`RouteConfigSet.from_dict` at
`src/medre/config/routes.py:845-879` is the top-level parser entry
point; it iterates `data["routes"]` in declaration order, calls
`RouteConfig.from_dict` for each entry, and finally invokes
`RouteConfigSet.validate` for duplicate-ID detection.

---

## 3. Current route expansion

### 3.1 Expansion entry points

All expansion lives in `src/medre/runtime/route_engine.py`. The public
entry point is :func:`build_runtime_routes` at
`src/medre/runtime/route_engine.py:713-751`, which delegates to the
private :func:`_expand_all_routes` at
`src/medre/runtime/route_engine.py:649-710`.

`_expand_all_routes` dispatches per enabled `RouteConfig` as follows
(`src/medre/runtime/route_engine.py:677-694`):

| Condition                          | Expansion function                                                           |
| ---------------------------------- | ---------------------------------------------------------------------------- |
| `rc.channel_room_map is not None`  | `_expand_channel_room_map_route(rc, adapter_platforms)`                      |
| `directionality == SOURCE_TO_DEST` | `_expand_route_config(rc)`                                                   |
| `directionality == DEST_TO_SOURCE` | `_expand_route_config(rc, swap_direction=True)`                              |
| `directionality == BIDIRECTIONAL`  | `_expand_route_config(rc)` + `_expand_route_config(rc, swap_direction=True)` |

Disabled routes (`enabled=False`) are skipped at
`src/medre/runtime/route_engine.py:678-680`. Expanded route IDs are
checked for collision against the provenance map at
`src/medre/runtime/route_engine.py:697-706`; the reserved suffix
patterns `__<N>`, `__rev_<N>`, and `__ch<channel>__<direction>` are
documented in the resulting error message.

### 3.2 Standard expansion: `_expand_route_config`

Defined at `src/medre/runtime/route_engine.py:389-478`. Behaviour:

1. Direction swap (`src/medre/runtime/route_engine.py:424-435`):
   when `swap_direction=True`, source/dest adapter tuples are swapped,
   source/dest channels are swapped, **and** the origin-label field
   is swapped from `rc.source_origin_label` to
   `rc.dest_origin_label`. This is the entire mechanism by which the
   reverse leg of a bidirectional route receives the dest-side label.

2. Per-source-adapter loop (`src/medre/runtime/route_engine.py:449-476`):
   - When there is exactly one source adapter and `swap_direction` is
     False, the expanded route ID equals `rc.route_id`
     (`src/medre/runtime/route_engine.py:453-454`).
   - When `swap_direction=True`, the ID is `f"{rc.route_id}__rev_{src_idx}"`
     (`src/medre/runtime/route_engine.py:455-456`).
   - Otherwise the ID is `f"{rc.route_id}__{src_idx}"`
     (`src/medre/runtime/route_engine.py:457-458`).
   - For each source adapter, one `RouteTarget(adapter=did, channel=dest_channel)`
     is created per dest adapter (`src/medre/runtime/route_engine.py:460`).
   - A single `RouteSource` is built carrying the (possibly swapped)
     `origin_label` (`src/medre/runtime/route_engine.py:462-467`).
   - The resulting `Route` is mutable (`Route` is `@dataclass` without
     `frozen=True` — see §3.4).

3. Policy conversion: `BridgePolicy.allowed_event_types` →
   `RouteSource.event_kinds` at
   `src/medre/runtime/route_engine.py:437-445`. The remaining
   allowlists become a core `RoutePolicy` via
   `_convert_bridge_policy` at
   `src/medre/runtime/route_engine.py:354-381`.

### 3.3 `channel_room_map` expansion: `_expand_channel_room_map_route`

Defined at `src/medre/runtime/route_engine.py:481-646`. Behaviour:

1. Cardinality check (`src/medre/runtime/route_engine.py:518-522`):
   the route requires exactly one source adapter and one dest
   adapter. (This is also enforced at config-parse time — see §2.4 —
   but the expansion function re-checks defensively for
   directly-constructed `RouteConfig` instances.)

2. Platform resolution (`src/medre/runtime/route_engine.py:524-567`):
   the function uses `adapter_platforms: dict[str, str]` to identify
   which of the two adapters is Matrix and which is Meshtastic. If
   either platform lookup fails, or the pair is not
   {matrix, meshtastic}, `RouteValidationError` is raised
   (`src/medre/runtime/route_engine.py:531-567`).

3. Direction / orientation matrix
   (`src/medre/runtime/route_engine.py:579-644`):
   - For each `(ch, room_id)` in `sorted(rc.channel_room_map.items())`
     (`src/medre/runtime/route_engine.py:582`), the function decides
     which legs to create based on `rc.directionality`:
     `SOURCE_TO_DEST` and `BIDIRECTIONAL` produce the forward leg;
     `DEST_TO_SOURCE` and `BIDIRECTIONAL` produce the reverse leg
     (`src/medre/runtime/route_engine.py:584-591`).
   - The `fwd_is_matrix_to_mesh` boolean
     (`src/medre/runtime/route_engine.py:552-561`) records whether
     the forward (source→dest) leg happens to be Matrix→Meshtastic
     or Meshtastic→Matrix, based on the declared source/dest
     orientation.

4. Matrix→Meshtastic leg construction
   (`src/medre/runtime/route_engine.py:601-622`):

   ```python
   fwd_id = f"{rc.route_id}__ch{ch}__matrix_to_meshtastic"
   if fwd_is_matrix_to_mesh:
       fwd_label = rc.source_origin_label
   else:
       fwd_label = rc.dest_origin_label
   routes.append(Route(
       id=fwd_id,
       source=RouteSource(
           adapter=matrix_id, event_kinds=event_kinds,
           channel=room_id, origin_label=fwd_label,
       ),
       targets=[RouteTarget(adapter=meshtastic_id, channel=ch)],
       enabled=rc.enabled, policy=route_policy,
   ))
   ```

5. Meshtastic→Matrix leg construction
   (`src/medre/runtime/route_engine.py:625-644`): the mirror image.
   The label assignment swaps:
   ```python
   rev_id = f"{rc.route_id}__ch{ch}__meshtastic_to_matrix"
   if fwd_is_matrix_to_mesh:
       rev_label = rc.dest_origin_label
   else:
       rev_label = rc.source_origin_label
   ```

**Key observation for the feature**: the label assignment is uniform
across every `(ch, room_id)` entry — the loop reads `rc.source_origin_label`
and `rc.dest_origin_label` directly, with no per-entry lookup. Every
expanded Matrix→Meshtastic leg from the same `channel_room_map`
inherits the same `fwd_label`; every Meshtastic→Matrix leg inherits
the same `rev_label`.

### 3.4 Expanded route / leg model

The expanded objects are core types from
`src/medre/core/routing/models.py`:

**`RouteSource`** (`src/medre/core/routing/models.py:28-57`) — frozen:

| Field          | Type              | Default | Notes                                                                                                       |
| -------------- | ----------------- | ------- | ----------------------------------------------------------------------------------------------------------- |
| `adapter`      | `str \| None`     | —       | Source adapter ID; `None` is wildcard.                                                                      |
| `event_kinds`  | `tuple[str, ...]` | —       | Empty tuple means "match any".                                                                              |
| `channel`      | `str \| None`     | —       | Source channel / room ID; `None` is wildcard.                                                               |
| `origin_label` | `str \| None`     | `None`  | Optional source-context origin label threaded from `RouteConfig`. `None` = unset; `""` = suppress fallback. |

**`RouteTarget`** (`src/medre/core/routing/models.py:97-118`) — frozen:

| Field         | Type                       | Default | Notes                                         |
| ------------- | -------------------------- | ------- | --------------------------------------------- |
| `adapter`     | `str \| None`              | `None`  | Target adapter ID.                            |
| `channel`     | `str \| None`              | `None`  | Target channel / room.                        |
| `destination` | `RouteDestination \| None` | `None`  | Identity-based destination (not used by CRM). |

**`Route`** (`src/medre/core/routing/models.py:126-162`) — **mutable**
(not frozen), so that the router can toggle `enabled` at runtime:

| Field             | Type                  | Default       | Notes                                          |
| ----------------- | --------------------- | ------------- | ---------------------------------------------- |
| `id`              | `str`                 | —             | Unique route ID.                               |
| `source`          | `RouteSource`         | —             | Frozen; replaced wholesale if mutated.         |
| `targets`         | `list[RouteTarget]`   | —             | Ordered fanout list.                           |
| `fanout_strategy` | `str`                 | `"broadcast"` | Only `broadcast` is supported.                 |
| `ownership`       | `str`                 | `"shared"`    | `shared` or `exclusive`.                       |
| `enabled`         | `bool`                | `True`        | Mutable; router honours this at match time.    |
| `policy`          | `RoutePolicy \| None` | `None`        | Core route-policy allowlist (post-conversion). |

**Critical shape fact**: `origin_label` lives on `RouteSource`, not on
`Route` or `RouteTarget`. This means the label is structurally
attached to the source-side filter and is naturally leg-specific: a
bidirectional expansion produces two `Route` objects, each with its
own `RouteSource`, each carrying its own `origin_label`. The same
shape applies to `channel_room_map` expansion — each per-channel,
per-direction leg is a separate `Route` with its own `RouteSource`.

### 3.5 Direction-aware label application today

Summarising the current label-assignment logic:

| Route shape                        | Direction      | Source of `RouteSource.origin_label`                                                                                             |
| ---------------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Standard                           | forward        | `rc.source_origin_label` (`src/medre/runtime/route_engine.py:435`)                                                               |
| Standard                           | reverse (swap) | `rc.dest_origin_label` (`src/medre/runtime/route_engine.py:429`)                                                                 |
| `channel_room_map` Matrix→Mesh leg | forward        | `rc.source_origin_label` when `fwd_is_matrix_to_mesh`, else `rc.dest_origin_label` (`src/medre/runtime/route_engine.py:605-608`) |
| `channel_room_map` Mesh→Matrix leg | reverse        | `rc.dest_origin_label` when `fwd_is_matrix_to_mesh`, else `rc.source_origin_label` (`src/medre/runtime/route_engine.py:627-630`) |

In all cases, the per-leg `RouteSource.origin_label` is a single value
read directly from `rc.source_origin_label` or `rc.dest_origin_label`.
There is no per-channel, per-room lookup.

---

## 4. Current label precedence

This section traces the exact code path from route expansion to
renderer output, with file:line citations for each link in the chain.

### 4.1 Step 1 — Expansion resolves per-leg label

See §3.5. The result is a :class:`~medre.core.routing.models.Route`
whose `source.origin_label` is one of:

- `None` (route-level label unset, both `source_origin_label` and
  `dest_origin_label` are `None`).
- `""` (explicit empty string — operator requested suppression of
  adapter fallback for this leg).
- A non-empty operator-defined string.

This value is frozen onto `RouteSource` at construction
(`src/medre/core/routing/models.py:57`,
`src/medre/runtime/route_engine.py:466`,
`src/medre/runtime/route_engine.py:616`,
`src/medre/runtime/route_engine.py:638`).

### 4.2 Step 2 — `deliver_to_target` threads label into render call

`TargetDeliveryService.deliver_to_target` receives the matched
expanded `Route` as its `route` parameter
(`src/medre/core/engine/pipeline/target_delivery.py:301-312`). When
it calls the rendering pipeline, it passes:

```python
rendering_result = await self._rendering_pipeline.render(
    _render_event,
    adapter_id or "",
    target.channel,
    target_platform=platform_param,
    max_text_chars=_max_text_chars,
    max_text_bytes=_max_text_bytes,
    delivery_strategy=_validated_strategy,
    capability_level=_capability_level,
    source_origin_label=route.source.origin_label,
)
```

(`src/medre/core/engine/pipeline/target_delivery.py:576-587`,
specifically `source_origin_label=route.source.origin_label` at
`src/medre/core/engine/pipeline/target_delivery.py:586`).

This is the **single point** where the matched route's label enters
the rendering pipeline. There is no other call site that populates
`source_origin_label` from a route.

### 4.3 Step 3 — `RenderingPipeline.render` freezes label onto context

`RenderingPipeline.render` builds a frozen `RenderingContext`
(`src/medre/core/rendering/renderer.py:540-549`):

```python
ctx = RenderingContext(
    delivery_strategy=strategy,
    target_adapter=target_adapter,
    target_channel=target_channel,
    target_platform=platform,
    max_text_chars=max_text_chars,
    max_text_bytes=max_text_bytes,
    capability_level=cap_level,
    source_origin_label=source_origin_label,
)
```

`RenderingContext.source_origin_label` is declared at
`src/medre/core/rendering/renderer.py:175` with default `None`. The
dataclass is frozen (`src/medre/core/rendering/renderer.py:89`), so
renderers cannot mutate it.

The pipeline passes `ctx` to every renderer's `can_render` and
`render` (`src/medre/core/rendering/renderer.py:551-563`).

### 4.4 Step 4 — Renderer resolves precedence

Each transport renderer implements the same precedence chain
locally. The canonical pattern is in the Meshtastic renderer
(`src/medre/adapters/meshtastic/renderer.py:384-393`):

```python
# Resolve origin_label precedence:
# ctx.source_origin_label (route/context) > source_attribution registry > None.
# NOTE: The target adapter config's origin_label is intentionally NOT used
# as a fallback — origin_label describes the message SOURCE, not the target.
# Use 'is not None' to preserve explicit empty string labels.
effective_origin_label: str | None = ctx.source_origin_label
if effective_origin_label is None:
    src_attr_cfg = self._source_attribution.get(event.source_adapter)
    if src_attr_cfg is not None:
        effective_origin_label = getattr(src_attr_cfg, "origin_label", None)
```

Equivalent resolution sites in the other renderers:

- Matrix: `src/medre/adapters/matrix/renderer.py:221-258`
  (`_build_source_attribution` — `ctx.source_origin_label` overrides
  the registry value at
  `src/medre/adapters/matrix/renderer.py:241-242`).
- MeshCore: equivalent pattern, uses
  `ctx.source_origin_label` → registry fallback (see the renderer's
  `_format_prefix_for` call sites).
- LXMF: equivalent pattern.

The Matrix renderer additionally exposes
`_resolve_source_origin_label` at
`src/medre/adapters/matrix/renderer.py:147-155` for the mmrelay
`KEY_MESHNET` injection path, which uses the same precedence via
`derive_meshnet_value` (`src/medre/adapters/matrix/renderer.py:100-116`).

### 4.5 Step 5 — Label reaches the formatter

The resolved `effective_origin_label` is passed to the renderer's
internal prefix-formatting helper (e.g.
`MeshtasticRenderer._format_prefix_for(..., source_origin_label=...)`
at `src/medre/adapters/meshtastic/renderer.py:563`). That helper
builds a :class:`~medre.core.rendering.attribution.RelayAttribution`
via :func:`~medre.core.rendering.attribution.build_relay_attribution`,
which threads the label into `RelayAttribution.source_origin_label`
(`src/medre/core/rendering/attribution.py:326-405`, specifically
`src/medre/core/rendering/attribution.py:403-404`).

The shared formatter :func:`format_relay_prefix`
(`src/medre/core/rendering/attribution.py:242-318`) builds a variable
map via `_build_variable_map`
(`src/medre/core/rendering/attribution.py:151-195`). The map uses
`is not None` checks for label fields
(`src/medre/core/rendering/attribution.py:163-185`) so that an
explicit empty string is preserved as `""` rather than coalesced to
the adapter value. The `{origin_label}` template alias is mapped to
`source_origin_label` at
`src/medre/core/rendering/attribution.py:65-74`.

### 4.6 Explicit empty-string suppression

The `is not None` check at
`src/medre/core/rendering/attribution.py:163-185` is the mechanism
by which an explicit `""` suppresses the adapter fallback. Because
`""` is not `None`, it survives the variable-map build and is
substituted verbatim into the rendered prefix. This is tested
end-to-end for all four transports in
`tests/test_origin_label_precedence.py:693-811`.

The same `is not None` semantics are replicated at the renderer level
(e.g. `src/medre/adapters/meshtastic/renderer.py:389-393`:
`effective_origin_label = ctx.source_origin_label; if
effective_origin_label is None: ...`). An explicit `""` therefore
short-circuits the registry lookup.

### 4.7 Source-attribution registry construction

The registry consulted in Step 4 is
`dict[str, SourceAttributionConfig]`, built by
`RuntimeBuilder._register_adapter_renderers` at
`src/medre/runtime/builder.py:243-439` (specifically the loop at
`src/medre/runtime/builder.py:371-377`):

```python
for _platform, _cfg_map in _all_config_maps:
    for _aid, _cfg in _cfg_map.items():
        source_attribution[_aid] = SourceAttributionConfig(
            adapter_id=_aid,
            platform=_platform,
            origin_label=getattr(_cfg, "origin_label", ""),
        )
```

`SourceAttributionConfig` is defined at
`src/medre/runtime/builder.py:69-91`. Its `origin_label` field
defaults to `""` (empty string), which is the value used when an
adapter config does not declare `origin_label`.

The registry is passed to each renderer at construction
(`src/medre/runtime/builder.py:386-429`) and is consulted at render
time as the second-level fallback.

---

## 5. Current `channel_room_map` behavior

### 5.1 Expansion cardinality

For a `channel_room_map` with N entries and `directionality=bidirectional`,
:func:`_expand_channel_room_map_route` produces `2 * N` expanded
:class:`Route` objects: one Matrix→Meshtastic leg and one
Meshtastic→Matrix leg per entry. With `directionality=source_to_dest`
or `dest_to_source`, it produces `N` legs.

Verified by `tests/test_routes_channel_room_map.py:337-415` and
`tests/test_routes_channel_room_map.py:463-478`.

### 5.2 Per-entry labels: NOT supported

The current `channel_room_map` value is a flat `dict[str, str]`. The
expansion loop at `src/medre/runtime/route_engine.py:582` destructures
each entry as `for ch, room_id in sorted(rc.channel_room_map.items())`.
The variables available in the loop body are:

- `ch` — the Meshtastic channel index string ("0"–"7").
- `room_id` — the canonical Matrix room ID string.
- The route-level `rc.source_origin_label` and `rc.dest_origin_label`
  (uniform across all entries).

There is no per-entry label. Every Matrix→Meshtastic leg from this
route receives the same `fwd_label`, and every Meshtastic→Matrix leg
receives the same `rev_label`
(`src/medre/runtime/route_engine.py:605-608`,
`src/medre/runtime/route_engine.py:627-630`).

### 5.3 Where labels are too coarse

The coarseness is structural, not accidental:

1. **Config layer** (`src/medre/config/routes.py:429`): the value type
   is `dict[str, str]`. There is no syntax for attaching a label to a
   specific entry. Operators who want per-channel labels today have to
   decompose the map into N separate routes, each with its own
   `source_origin_label` / `dest_origin_label` (this is the workaround
   documented in `docs/spec/routing-delivery.md:1366-1371` and
   `docs/dev/relay-prefix-attribution-audit.md:709-715`).

2. **Expansion layer** (`src/medre/runtime/route_engine.py:601-644`):
   the label values are read from `rc.source_origin_label` /
   `rc.dest_origin_label` inside the per-entry loop, but those values
   are constant across iterations. The loop does not consult the map
   entry for label data.

3. **Rendering layer**: no coarseness here. The renderer precedence
   chain (`src/medre/adapters/meshtastic/renderer.py:384-393`,
   `src/medre/adapters/matrix/renderer.py:221-258`) already operates
   per-delivery, so once the per-leg `RouteSource.origin_label` is
   populated correctly, the rest of the pipeline works without
   modification.

The gap is therefore confined to the config model and the
`channel_room_map` expansion function.

### 5.4 Verified behavior tests

- Per-direction label correctness for `channel_room_map`:
  `tests/test_routes_channel_room_map_origin_label.py:106-164`
  (forward leg carries `source_origin_label`, reverse leg carries
  `dest_origin_label`, in both platform orientations).
- Sentinel-state coverage (unset → `None`, explicit `""` → `""`):
  `tests/test_routes_channel_room_map_origin_label.py:246-305`.
- Direction-selectivity (`source_to_dest` only carries source label,
  `dest_to_source` only carries dest label):
  `tests/test_routes_channel_room_map_origin_label.py:315-362`.
- Multi-channel uniform label application:
  `tests/test_routes_channel_room_map_origin_label.py:220-234`
  (every expanded channel leg gets the direction-correct label —
  which is the very uniformity the upcoming feature must break
  opt-in-by-entry).

---

## 6. Available source-context identifiers during expansion

For each identifier, the table records whether it is available at
:func:`_expand_channel_room_map_route` call time and the file:line
where it is read.

| Identifier                             | Available? | Read at (file:line)                                                                                                                                                                                                                                                          |
| -------------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source adapter id                      | Yes        | `rc.source_adapters[0]` at `src/medre/runtime/route_engine.py:524` (or swapped at `src/medre/runtime/route_engine.py:425`)                                                                                                                                                   |
| Dest adapter id                        | Yes        | `rc.dest_adapters[0]` at `src/medre/runtime/route_engine.py:525` (or swapped at `src/medre/runtime/route_engine.py:426`)                                                                                                                                                     |
| Source channel id (Meshtastic channel) | Yes        | `ch` from the per-entry loop at `src/medre/runtime/route_engine.py:582`; used as `RouteTarget.channel` on the Mesh→Matrix leg at `src/medre/runtime/route_engine.py:618` and as `RouteSource.channel` on the Mesh→Matrix leg at `src/medre/runtime/route_engine.py:637`      |
| Dest channel id                        | Yes        | Same `ch`, used symmetrically on the Matrix→Mesh leg (target channel at `src/medre/runtime/route_engine.py:618`; source channel at `src/medre/runtime/route_engine.py:637` is the room, not the channel)                                                                     |
| Matrix room id                         | Yes        | `room_id` from the per-entry loop at `src/medre/runtime/route_engine.py:582`; used as `RouteSource.channel` on the Matrix→Mesh leg at `src/medre/runtime/route_engine.py:615` and as `RouteTarget.channel` on the Mesh→Matrix leg at `src/medre/runtime/route_engine.py:640` |
| Meshtastic channel index (numeric)     | Yes        | Same as source/dest channel id above; parsed and validated to int 0–7 at config time (`src/medre/config/routes.py:658-671`) and stored as `str(int)` in the normalised map.                                                                                                  |
| Route id                               | Yes        | `rc.route_id` (`src/medre/config/routes.py:417`); used to build expanded IDs at `src/medre/runtime/route_engine.py:602`, `src/medre/runtime/route_engine.py:626`                                                                                                             |
| Per-entry `source_origin_label`        | **No**     | Not present in the config model; the map value is `str`, not a table.                                                                                                                                                                                                        |
| Per-entry `dest_origin_label`          | **No**     | Not present in the config model; same as above.                                                                                                                                                                                                                              |

The `channel_room_map` entries currently expose `ch` and `room_id`
only. The feature work must extend the entry shape to carry
optional `source_origin_label` and `dest_origin_label` per entry,
with sensible defaults (fall back to the route-level labels, then to
`None`).

---

## 7. What must remain unchanged

The following invariants are currently enforced and the upcoming
feature is required not to break them. Each is cited at its enforcement site.

### 7.1 `origin_label` semantic invariants

| Invariant                                                    | Enforcement (file:line)                                                                                                                                                             |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `origin_label` is NOT delivery evidence.                     | Spec: `docs/spec/routing-delivery.md:1243`. No code stores it in `DeliveryReceipt` or `RenderingEvidence`.                                                                          |
| `origin_label` is NOT a routing key.                         | Spec: `docs/spec/routing-delivery.md:1244`. `RouteSource` matching uses `adapter`, `event_kinds`, `channel` only (`src/medre/core/routing/router.py:154` `match()`).                |
| `origin_label` is NOT a native transport identity.           | Spec: `docs/spec/routing-delivery.md:1245-1246`. Adapter projection helpers (`src/medre/adapters/_attribution_dispatch.py:183-257`) never read `origin_label` from native metadata. |
| `origin_label` is operator-defined, set at config load time. | Spec: `docs/spec/routing-delivery.md:1219-1232`.                                                                                                                                    |

### 7.2 Precedence chain invariants

| Invariant                                                                  | Enforcement (file:line)                                                                                                                                        |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Precedence order: route/context > adapter registry > empty string.         | `src/medre/adapters/meshtastic/renderer.py:384-393`; `src/medre/adapters/matrix/renderer.py:221-258`; spec `docs/spec/routing-delivery.md:1234-1239`.          |
| Explicit `""` suppresses adapter fallback (use `is not None`, not truthy). | `src/medre/core/rendering/attribution.py:163-185`; `src/medre/adapters/meshtastic/renderer.py:389-393`; tests `tests/test_origin_label_precedence.py:693-811`. |
| `RenderingContext.source_origin_label` is frozen on an immutable context.  | `src/medre/core/rendering/renderer.py:89`, `src/medre/core/rendering/renderer.py:175`.                                                                         |
| The renderer protocol signature is fixed: `render(event, ctx)`.            | `src/medre/core/rendering/renderer.py:274-346`.                                                                                                                |

### 7.3 Config-model invariants

| Invariant                                                                                                | Enforcement (file:line)                                                         |
| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `channel_room_map` is mutually exclusive with `source_channel`/`dest_channel`/`source_room`/`dest_room`. | `src/medre/config/routes.py:607-621`.                                           |
| `channel_room_map` requires exactly one source and one dest adapter.                                     | `src/medre/config/routes.py:622-634`.                                           |
| Channel keys are integers 0–7 after normalisation.                                                       | `src/medre/config/routes.py:658-671`.                                           |
| Duplicate channel keys (after normalisation) are rejected.                                               | `src/medre/config/routes.py:673-678`.                                           |
| Room values are non-empty strings starting with `!` (not `#`).                                           | `src/medre/config/routes.py:682-704`.                                           |
| Duplicate room values: not rejected at config parse; ambiguity validated at runtime expansion.           | `src/medre/runtime/route_engine.py` (`_validate_duplicate_rooms_for_direction`) |
| Empty `channel_room_map` is rejected.                                                                    | `src/medre/config/routes.py:716-721`.                                           |
| `source_origin_label` / `dest_origin_label` must be strings (bool rejected before generic type check).   | `src/medre/config/routes.py:561-595`.                                           |
| `RouteConfig` is frozen.                                                                                 | `src/medre/config/routes.py:363`.                                               |

### 7.4 Expansion invariants

| Invariant                                                                                                 | Enforcement (file:line)                                                                   |
| --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `_expand_channel_room_map_route` requires exactly one source and one dest.                                | `src/medre/runtime/route_engine.py:518-522`.                                              |
| `channel_room_map` requires one Matrix and one Meshtastic adapter.                                        | `src/medre/runtime/route_engine.py:545-567`.                                              |
| Expanded route IDs follow the deterministic `__ch{ch}__{direction}` pattern.                              | `src/medre/runtime/route_engine.py:602`, `src/medre/runtime/route_engine.py:626`.         |
| Expanded route ID collisions raise `RouteValidationError`.                                                | `src/medre/runtime/route_engine.py:697-706`.                                              |
| Direction-aware label assignment: forward leg → `source_origin_label`, reverse leg → `dest_origin_label`. | `src/medre/runtime/route_engine.py:605-608`, `src/medre/runtime/route_engine.py:627-630`. |

### 7.5 Rendering-pipeline invariants

| Invariant                                                                                     | Enforcement (file:line)                                                                      |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `RenderingPipeline.render` accepts `source_origin_label` as keyword-only.                     | `src/medre/core/rendering/renderer.py:455-467`.                                              |
| `source_origin_label=None` leaves the context field `None`.                                   | `src/medre/core/rendering/renderer.py:540-549`.                                              |
| `format_relay_prefix` never raises.                                                           | `src/medre/core/rendering/attribution.py:274-318` (try/except wraps the whole body).         |
| `RelayAttribution.source_origin_label` is the canonical field; `{origin_label}` is its alias. | `src/medre/core/rendering/attribution.py:140`, `src/medre/core/rendering/attribution.py:73`. |

### 7.6 Source-attribution registry invariants

| Invariant                                                                   | Enforcement (file:line)                                                                               |
| --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Registry is built once at assembly time from adapter configs.               | `src/medre/runtime/builder.py:339-377`.                                                               |
| Registry maps adapter ID → `SourceAttributionConfig`.                       | `src/medre/runtime/builder.py:69-91`.                                                                 |
| Renderers consult the registry only when `ctx.source_origin_label is None`. | `src/medre/adapters/meshtastic/renderer.py:389-393`; `src/medre/adapters/matrix/renderer.py:237-242`. |

### 7.7 Identity projection invariants

| Invariant                                                           | Enforcement (file:line)                                                                                                       |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Core rendering never imports adapter packages or reads native keys. | `src/medre/core/rendering/attribution.py:1-16`; the dispatch boundary is `src/medre/adapters/_attribution_dispatch.py:1-257`. |
| Per-transport sender projection is owned by each adapter package.   | `src/medre/adapters/matrix/attribution.py`, `src/medre/adapters/meshtastic/attribution.py`, etc.                              |
| The runtime injects the projection callback into core planning.     | `src/medre/runtime/builder.py:447-494` (`_build_project_sender_metadata_fn`).                                                 |

The upcoming feature work should not add native-key inspection to
core rendering, should not change the projection dispatch signature,
and should not add transport-specific branching to
`_expand_channel_room_map_route` beyond the existing Matrix /
Meshtastic platform identification.

---

## 8. Gap analysis and recommendation

### 8.1 Config model changes

The minimal, backward-compatible change is to make the
`channel_room_map` value polymorphic:

- **Current shape** (continues to work unchanged):
  ```yaml
  channel_room_map:
    "0": "!room0:example.com"
    "1": "!room1:example.com"
  ```
- **Proposed extended shape** (opt-in per entry):
  ```yaml
  channel_room_map:
    "0":
      room: "!room0:example.com"
      source_origin_label: "Ops Channel"
      dest_origin_label: "Radio 0"
    "1": "!room1:example.com" # bare-string form still accepted
  ```

The value type becomes `dict[str, str | dict[str, str]]` at the YAML
layer and `dict[str, ChannelRoomMapEntry]` at the dataclass layer,
where `ChannelRoomMapEntry` is a new frozen dataclass with fields:

| Field                 | Type          | Default | Notes                                                            |
| --------------------- | ------------- | ------- | ---------------------------------------------------------------- |
| `room`                | `str`         | —       | Canonical Matrix room ID, same validation as today.              |
| `source_origin_label` | `str \| None` | `None`  | Per-entry forward-leg label. `None` = inherit route-level label. |
| `dest_origin_label`   | `str \| None` | `None`  | Per-entry reverse-leg label. `None` = inherit route-level label. |

The parser at `src/medre/config/routes.py:597-721` detects whether each
map value is a `str` (legacy) or a `dict` (new form), normalises both
to `ChannelRoomMapEntry`, and runs the existing room validation on the
`room` sub-field. The existing channel-key, duplicate-channel,
alias-rejection, and canonical-room-ID checks are unchanged. Duplicate
room values are no longer rejected at config parse; the duplicate-room
ambiguity check moved to runtime expansion
(`src/medre/runtime/route_engine.py`,
`_validate_duplicate_rooms_for_direction`), where it rejects duplicate
rooms only when a Matrix→Meshtastic leg would be created.

The proposed field names `source_origin_label` and
`dest_origin_label` match the existing route-level field names at
`src/medre/config/routes.py:430-431` exactly — no new vocabulary is
introduced. Re-using the existing names keeps the operator mental
model uniform: "the entry-level label has the same semantics as the
route-level label, just scoped to this entry".

### 8.2 Expansion changes

:func:`_expand_channel_room_map_route` at
`src/medre/runtime/route_engine.py:481-646` must be updated:

1. The per-entry loop at `src/medre/runtime/route_engine.py:582`
   destructures `ch, entry` where `entry` is the new
   `ChannelRoomMapEntry`. The `room_id` variable becomes `entry.room`.

2. The forward-leg label assignment at
   `src/medre/runtime/route_engine.py:605-608` becomes a two-level
   lookup:

   ```python
   if fwd_is_matrix_to_mesh:
       fwd_label = entry.source_origin_label
       if fwd_label is None:
           fwd_label = rc.source_origin_label
   else:
       fwd_label = entry.dest_origin_label
       if fwd_label is None:
           fwd_label = rc.dest_origin_label
   ```

   This preserves the current behaviour when the entry has no label
   (entry label is `None` → route-level label wins) and adds the
   per-entry override when present. Per-entry label takes precedence
   over route-level label because it is more specific.

3. The reverse-leg label assignment at
   `src/medre/runtime/route_engine.py:627-630` receives the symmetric
   treatment.

4. The expanded route ID pattern, directionality handling, platform
   identification, policy conversion, and provenance tracking remain
   unchanged.

The new precedence chain (after the feature) becomes:

1. Per-entry label (if set on the matched `channel_room_map` entry).
2. Route-level label (`source_origin_label` / `dest_origin_label`).
3. Source-adapter `origin_label` from the registry.
4. Empty string.

Steps 3 and 4 are unchanged from §4.

### 8.3 RenderingContext changes

**None required.** `RenderingContext.source_origin_label`
(`src/medre/core/rendering/renderer.py:175`) already carries the
resolved per-leg value. The existing call site at
`src/medre/core/engine/pipeline/target_delivery.py:586` already
reads `route.source.origin_label`, which will be populated from the
per-entry label by the updated expansion function. No renderer,
formatter, or `RenderingPipeline.render` signature change is needed.

This is the most important architectural property of the proposed
design: the feature is **confined to the config model and the
expansion function**. The downstream pipeline is label-source-agnostic.

### 8.4 Storage / wire / evidence changes

**None required.** `origin_label` is not persisted to delivery
receipts, outbox metadata, or wire payloads (spec
`docs/spec/routing-delivery.md:1243-1246`). The expanded `Route` is
an operational artifact, not a stored canonical event
(`docs/spec/routing-delivery.md:289`).

### 8.5 Existing test coverage

| File                                                 | Coverage                                                                                               |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `tests/test_routes.py`                               | `RouteConfig` parsing, including `source_origin_label` / `dest_origin_label` (lines 149-191, 261-315). |
| `tests/test_route_origin_label_context.py`           | Direction-aware label plumbing for standard expansion; RenderingContext propagation (full file).       |
| `tests/test_origin_label_precedence.py`              | Per-renderer precedence (Matrix, Meshtastic, MeshCore, LXMF), empty-string suppression (full file).    |
| `tests/test_routes_channel_room_map.py`              | `channel_room_map` config validation and runtime expansion without per-entry labels (full file).       |
| `tests/test_routes_channel_room_map_origin_label.py` | Direction-aware label correctness for `channel_room_map` (uniform per-entry labels) (full file).       |
| `tests/test_runtime_builder_routes.py`               | `RuntimeBuilder.build()` route registration including `channel_room_map` integration (full file).      |

### 8.6 Missing test coverage

The implementation waves must add tests for:

1. **Config parsing — extended value shape**:
   - Mixed map (one bare-string entry, one table entry) parses to the
     expected `ChannelRoomMapEntry` instances.
   - Unknown keys in the table form are rejected.
   - Non-string `source_origin_label` / `dest_origin_label` values
     (bool, int) are rejected — same rule as route-level labels.
   - Empty-string `source_origin_label=""` and `dest_origin_label=""`
     on an entry are accepted and preserved verbatim (sentinel for
     "suppress adapter fallback for this entry").
   - Table form missing the `room` key is rejected.
   - Table form with empty / blank `room` is rejected.
   - Bare-string form continues to parse unchanged (backward
     compatibility).

2. **Expansion — per-entry label threading**:
   - Entry-level label overrides route-level label for the matching
     leg only (other legs keep the route-level label).
   - Entry-level `source_origin_label=None` falls through to the
     route-level `source_origin_label`.
   - Entry-level `dest_origin_label=None` falls through to the
     route-level `dest_origin_label`.
   - Entry-level `""` is preserved as `RouteSource.origin_label == ""`
     (does NOT fall through to the route-level label — explicit
     suppression).
   - Mixed map (some entries with labels, some without) produces the
     expected per-leg labels.

3. **End-to-end rendering** (optional but recommended):
   - A delivery through a `channel_room_map` with per-entry labels
     produces a rendered prefix whose `{origin_label}` matches the
     per-entry value, not the route-level value.

4. **Regression — backward compatibility**:
   - Every existing test in
     `tests/test_routes_channel_room_map.py` and
     `tests/test_routes_channel_room_map_origin_label.py` continues
     to pass unchanged (bare-string map values must still produce
     the same expansion).

### 8.7 Risks and sequencing recommendations

**Risk 1: Breaking the bare-string form.** The polymorphic value
parse is the riskiest change. The implementation guards the
"legacy string" path with an explicit `isinstance(raw_value, str)`
check before attempting the "new table" path, and the table path
rejects any value that is not a `dict`. The existing test suite
at `tests/test_routes_channel_room_map.py` is the regression net.

**Risk 2: Confusing per-entry `""` with per-entry `None`.** Both
must be preserved verbatim through to `RouteSource.origin_label`,
matching the route-level semantics
(`src/medre/config/routes.py:561-595`). The renderer-side `is not
None` check (`src/medre/adapters/meshtastic/renderer.py:389-393`)
already handles both correctly — but only if the expansion function
threads them through unchanged. A naive `entry.source_origin_label
or rc.source_origin_label` would treat `""` as falsy and incorrectly
fall through. Use explicit `is None` checks.

**Risk 3: Documentation drift.** The spec at
`docs/spec/routing-delivery.md:1366-1371` and the audit at
`docs/dev/relay-prefix-attribution-audit.md:709-715` both currently
state that per-channel labels are not implemented. Both must be
updated in the same change that ships the feature, per the AGENTS.md
"Runtime semantic changes require the relevant `docs/spec` page,
schema updates in `docs/schemas`, tests, and a fragment under
`docs/changes/unreleased/NNN-brief-description.md` in the same
change" rule (`AGENTS.md:50-54`).

**Recommended implementation sequencing:**

1. **Config model.** Add `ChannelRoomMapEntry`, extend
   `RouteConfig.from_dict` to parse the polymorphic value shape, keep
   the existing `channel_room_map: dict[str, ?]` attribute typed as
   `dict[str, ChannelRoomMapEntry]` (always normalised). Add parsing
   tests (§8.6 item 1). No expansion changes yet — existing expansion
   tests must still pass because the entry's `room` field carries the
   same data.

2. **Expansion.** Update
   `_expand_channel_room_map_route` to read per-entry labels with
   the two-level lookup described in §8.2. Add expansion tests
   (§8.6 item 2). Update the existing
   `tests/test_routes_channel_room_map_origin_label.py` tests if
   any helper constructs `RouteConfig` with the old `dict[str, str]`
   shape directly — they should continue to work via the parser
   normalisation path.

3. **Docs and changelog.** Update
   `docs/spec/routing-delivery.md` §17.5.8 (remove the "not
   implemented" note, document the new shape). Update
   `docs/dev/relay-prefix-attribution-audit.md` §9. Add a change
   fragment under `docs/changes/unreleased/`. Update
   `docs/schemas/` if a JSON Schema exists for routes.

4. **End-to-end test (optional).** Add a single
   integration-style test that exercises a delivery through a
   `channel_room_map` with per-entry labels and asserts the rendered
   prefix uses the per-entry value.

5. **Audit update.** Update this document to reflect the
   shipped behaviour (remove the "gap" framing, document the new
   precedence chain).

The RenderingContext, renderer, formatter, source-attribution
registry, target-delivery service, and storage layers require NO
changes for this feature. The entire feature is structurally confined to
the config and expansion layers.

---

## Inspected files

### Source code

| File                                                | Status                                                   |
| --------------------------------------------------- | -------------------------------------------------------- |
| `src/medre/config/routes.py`                        | Full                                                     |
| `src/medre/runtime/route_engine.py`                 | Full                                                     |
| `src/medre/runtime/builder.py`                      | Full                                                     |
| `src/medre/core/routing/models.py`                  | Full                                                     |
| `src/medre/core/routing/router.py`                  | Grepped (match signature only)                           |
| `src/medre/core/rendering/renderer.py`              | Full                                                     |
| `src/medre/core/rendering/attribution.py`           | Full                                                     |
| `src/medre/core/engine/pipeline/target_delivery.py` | Partial (deliver_to_target signature + render call site) |
| `src/medre/adapters/_attribution_dispatch.py`       | Full                                                     |
| `src/medre/adapters/matrix/renderer.py`             | Grepped (origin_label precedence sites)                  |
| `src/medre/adapters/meshtastic/renderer.py`         | Partial (origin_label resolution block)                  |

### Spec / docs

| File                                         | Status                       |
| -------------------------------------------- | ---------------------------- |
| `docs/spec/routing-delivery.md`              | Partial (§2, §17.5.2-17.5.8) |
| `docs/dev/relay-prefix-attribution-audit.md` | Full                         |
| `docs/dev/testing.md`                        | Full                         |
| `AGENTS.md`                                  | Full                         |

### Test files

| File                                                 | Focus                                                    |
| ---------------------------------------------------- | -------------------------------------------------------- |
| `tests/test_routes.py`                               | RouteConfig parsing, validation, label fields            |
| `tests/test_route_origin_label_context.py`           | Standard expansion label plumbing; RenderingContext      |
| `tests/test_origin_label_precedence.py`              | Per-renderer precedence; empty-string suppression        |
| `tests/test_routes_channel_room_map.py`              | channel_room_map config validation + runtime expansion   |
| `tests/test_routes_channel_room_map_origin_label.py` | Direction-aware labels for channel_room_map (uniform)    |
| `tests/test_runtime_builder_routes.py`               | RuntimeBuilder route registration incl. channel_room_map |

---

## 9. Implementation Status

The gap described in §8 is closed. Per-entry origin labels for
`channel_room_map` are implemented and shipped; the precedence chain
documented in §8.2 (per-entry label > route-level label > adapter
`origin_label` > empty string) is live.

Key implementation sites:

- `src/medre/config/routes.py` — `ChannelRoomMapEntry` (frozen
  dataclass with `room`, `source_origin_label`, `dest_origin_label`)
  and the polymorphic parse in `RouteConfig.from_dict`. Each map
  value is accepted as a bare room-ID string or a structured table;
  unknown keys, non-string labels, and booleans are rejected.
- `src/medre/runtime/route_engine.py` —
  `_expand_channel_room_map_route` resolves the effective per-leg label
  with an `is not None` lookup so an explicit empty string suppresses
  the adapter fallback and an unset label falls through to the
  route-level value.

The RenderingContext, renderer, formatter, source-attribution registry,
target-delivery service, and storage layers required no changes, as
predicted in §8.3 and §8.4. Normative wording lives in
`docs/spec/routing-delivery.md` §17.5.8; operator guidance lives in
`docs/ops/configuration.md`.
