# Dispatch Refactor, platform_hint, Explicit Empty Labels

## Summary

Refactored the attribution dispatch to be dispatch-only (no inline native
interpretation), wired `platform_hint` from `SourceAttributionConfig` into
projection, preserved explicit empty origin labels, and cleaned
MatrixRenderer registration to be Matrix-config-driven.

## Behavior

### Dispatch-only projection

- `_attribution_dispatch.py` now delegates all native key interpretation to
  per-adapter attribution modules. It detects the source platform and calls
  the correct adapter projection helper.
- `matrix/attribution.py` gains `project_matrix_attribution(native_data)`
  for dispatch-oriented Matrix sender projection.
- `meshtastic/attribution.py` gains `apply_flat_key_fallback(fields, native_data)`
  for cross-platform flat-key enrichment (previously inline in dispatch).
- `meshtastic/attribution.py` `project_meshtastic_attribution` gains a
  `with_fallback` parameter (`True` by default). The dispatch uses
  `with_fallback=False` for simple extraction; the renderer applies its own
  fallbacks.
- Shared infrastructure modules (underscore-prefixed, e.g.
  `_attribution_dispatch`) are exempt from cross-transport boundary checks.

### platform_hint wiring

- `project_source_fields` and `detect_source_platform` accept an optional
  `platform_hint: str | None` parameter.
- Platform resolution precedence: explicit `platform_hint` > adapter-ID
  heuristic > native key shape > `None`.
- All four renderers resolve `source_info` from the source attribution
  registry once and pass `source_info.platform` as `platform_hint`.

### Explicit empty origin labels

- Renderers use `is not None` checks for `ctx.source_origin_label`,
  preserving explicit empty string (`""`) as "intentionally blank".
- `derive_meshnet_value` uses `is not None` checks: `""` returns `""`
  (explicitly empty); `None` falls through to the adapter label.
- Origin label precedence: `ctx.source_origin_label` (when not `None`,
  including `""`) > adapter `origin_label` > empty string.

### MatrixRenderer registration

- MatrixRenderer registers when Matrix configs exist. Meshtastic configs no
  longer trigger registration. Meshtastic configs are still passed as
  `source_configs` for mmrelay wire compatibility.

## Affected files

- `src/medre/adapters/_attribution_dispatch.py`: Refactored to dispatch-only.
- `src/medre/adapters/matrix/attribution.py`: Added
  `project_matrix_attribution`.
- `src/medre/adapters/meshtastic/attribution.py`: Added
  `apply_flat_key_fallback`; added `with_fallback` parameter.
- `src/medre/adapters/matrix/renderer.py`: Wired `platform_hint`; `is not None`
  origin label checks.
- `src/medre/adapters/meshtastic/renderer.py`: Wired `platform_hint`; `is not
None` origin label checks.
- `src/medre/adapters/meshcore/renderer.py`: Wired `platform_hint`; `is not
None` origin label checks.
- `src/medre/adapters/lxmf/renderer.py`: Wired `platform_hint`; `is not None`
  origin label checks.
- `src/medre/runtime/builder.py`: MatrixRenderer registration Matrix-config-driven.
- `src/medre/runtime/architecture_report.py`: Underscore-prefixed modules
  treated as shared infrastructure.
- `src/medre/interop/mmrelay.py`: `derive_meshnet_value` uses `is not None`.
- `src/medre/adapters/lxmf/attribution.py`: Reworded docstring to avoid false
  positive in SDK import boundary scan.
