# Adapter registration reference

`medre.adapter_spec.AdapterSpec` is the declarative seam between an
independently distributed provider and core. Publish a zero-argument factory in
the `medre.adapters` entry-point group; its entry-point name equals `transport`.
The factory returns an `AdapterSpec`. Import registration value types from
`medre.adapter_spec`, which never triggers discovery; direct factory imports
therefore cannot rediscover a partially initialized factory. Its module imports only SDK-free
declarations. Use `SymbolRef(module, name)` for deferred implementation access;
do not query the registry while its factories are being discovered.

Distribution names, Python package names, and repository hosts are independent
of the transport token. The token follows `[a-z][a-z0-9_-]*`. Discovery happens
once per process. Core never installs or downloads providers.

## Fields and loading phases

The constructor requires the fields through `attribution_projector`, even where
their value may be `None` or empty. Remaining fields have the defaults shown.

| Field                        | Value and contract                                                                                                                                                                                                                                                                          | Loaded by                                                                                     |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `transport`                  | Unique transport token matching the entry-point name.                                                                                                                                                                                                                                       | Discovery.                                                                                    |
| `config`                     | `SymbolRef` to an SDK-free native config class with `from_dict(mapping)` and `validate()`. It exposes `adapter_id`; parsing injects the instance ID.                                                                                                                                        | Config loader and environment overrides.                                                      |
| `runtime_config`             | `SymbolRef` to an SDK-free lifecycle wrapper with `from_dict(adapter_id, mapping)`, or `None` to use `GenericAdapterRuntimeConfig`. Wrappers expose `adapter_id`, `enabled`, `adapter_kind`, and `config`.                                                                                  | Config loader and environment overrides.                                                      |
| `adapter`                    | `SymbolRef` to the real adapter constructor called with the prepared native config as its sole positional argument; return an `AdapterContract`.                                                                                                                                            | Runtime construction after dependency availability succeeds.                                  |
| `renderer_factory`           | `SymbolRef` to the keyword-only factory described below, or `None` for core's text-renderer fallback.                                                                                                                                                                                       | Runtime assembly; SDK-free, including fake-only runs.                                         |
| `dependency_probe`           | `SymbolRef` to a **boolean value**, or `None` when no native Python SDK is required. This is not a callback.                                                                                                                                                                                | Real adapter construction and SDK diagnostics.                                                |
| `distribution`               | Native SDK distribution name, or `None`; this is **not** the adapter distribution name.                                                                                                                                                                                                     | SDK display/diagnostics. Provider package identity comes from installed entry-point metadata. |
| `import_names`               | Tuple of SDK import names; diagnostic alternatives when no explicit probe is available. Empty is valid for sidecars or standard-library clients.                                                                                                                                            | SDK diagnostics.                                                                              |
| `sdk_import_roots`           | Tuple of SDK import roots for architecture checks and default `WARNING` dependency logging. Empty is valid.                                                                                                                                                                                 | Architecture/logging policy.                                                                  |
| `native_namespace_reader`    | `SymbolRef` to `reader(native_data) -> Mapping`, interpreting only the provider's current native schema version. Return `{}` for unsupported/absent data.                                                                                                                                   | Native metadata projection.                                                                   |
| `versioned_namespace_reader` | `SymbolRef` to `reader(native_data) -> Mapping`, accepting positive integer native versions for detection, including future versions without interpreting their fields.                                                                                                                     | Platform detection.                                                                           |
| `attribution_projector`      | `SymbolRef` to `projector(native_data, *, source_transport_id=None) -> dict`. Interpret only owned native fields and return generic attribution values.                                                                                                                                     | Source attribution rendering.                                                                 |
| `fake_adapter`               | Optional `SymbolRef`, default `None`. Its constructor accepts `adapter_id=` as a keyword without any other required arguments and returns an `AdapterContract`. Extra optional native arguments are allowed. Construction/start/delivery require no SDK, network, credentials, or hardware. | Fake runtime construction and lifecycle drills.                                               |
| `runtime_config_preparer`    | Optional `SymbolRef`, default `None`, to `prepare(config, *, adapter_id, paths, routes) -> config`. Pure, SDK-free preflight for all enabled fake/real instances. Failure aborts runtime assembly and guided bridge authoring. Derived values are not saved by authoring. | Runtime builder before adapter construction; guided bridge preview before saving. |
| `runtime_directories`        | Optional `SymbolRef`, default `None`, to `directories(config, *, adapter_id, paths) -> Iterable[Path]`. Declare additional required state directories; core creates them.                                                                                                                   | Runtime startup. Core always creates the generic per-instance state root.                     |
| `cli_register`               | Optional `SymbolRef`, default `None`, to `register(parser: argparse.ArgumentParser) -> None`. Configure the core-owned `medre adapter <transport>` parser; declaration/help imports remain SDK-free.                                                                                        | CLI parser construction.                                                                      |
| `cli_dispatch`               | Optional `SymbolRef`, default `None`, to `dispatch(args) -> bool`. Return whether the provider handled the adapter command. Declare both CLI hooks or neither.                                                                                                                              | Adapter CLI dispatch.                                                                         |
| `support_endpoint_fields`    | Tuple of native config field names safe for endpoint diagnostics, default `()`. No duplicates.                                                                                                                                                                                              | Support evidence projection.                                                                  |
| `support_secret_fields`      | Tuple of credential field names to redact, default `()`. No duplicates; disjoint from endpoint fields.                                                                                                                                                                                      | Support evidence sanitization.                                                                |
| `native_detection_priority`  | Nonnegative integer, default `100`. Lower values win native-evidence ambiguity; registration order breaks ties. This does not prioritize transports for source injection.                                                                                                                   | Native namespace detection.                                                                   |
| `destinations`               | Tuple of `DestinationSpec(kind, validator)`, default `()`. Kind tokens are globally unique; `channel` is reserved to core. Pure validators accept a route destination and raise `ValueError` for invalid native addresses.                                                                  | Config validation.                                                                            |
| `sample_config`              | Optional `SymbolRef`, default `None`, to `sample_config() -> AdapterSample`. Native fake values and optional opaque channel; excludes lifecycle fields. Requires a declared fake to contribute to generated samples. The channel is shown as the format example in guided context prompts.                                                                        | `medre config sample`.                                                                        |
| `logger_levels`              | Tuple of `(logger_name, canonical_level_name)`, default `()`. Names must be nonempty and unique; `root`, `medre`, and `medre.*` are reserved. Levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.                                                                                | Dependency logging; explicit operator levels take precedence.                                 |

Renderer factories receive `runtime_configs`, `all_runtime_configs`, and
`source_attribution` as keywords. Values are the configured instance wrappers,
the full transport-to-wrapper-map, and core's generic attribution map. Return a
`Renderer` or `None`; core registers returned renderers at priority 50. A renderer
does not import another provider or an optional native SDK.

`runtime_config=None` has a deliberate generic fallback; it is not a dead field.
Dedicated wrappers satisfy the structural `AdapterRuntimeConfig` interface and
may be independent classes; inheriting `GenericAdapterRuntimeConfig` is optional.
Likewise, a provider without `runtime_directories` needs no additional directory
hook. Hooks declare actual provider requirements rather than mandating four
transport-specific shapes.

Core builds each CLI contribution in isolation before exposing its parser.
The registration callback adds arguments and subcommands to its own parser;
it does not register its transport name in a shared subparser collection.
Use provider-specific argument destinations. The `command`, `adapter_command`,
and `_adapter_cli_failures` destinations/defaults belong to core and are rejected
at every nesting level. Description, epilog, usage, and formatter customization
are preserved. Dispatch implementations keep SDK-touching imports inside the
selected command handler.

A failed CLI hook is omitted from the command tree and reported by
`medre adapters` and `medre adapters check`; preflight exits with configuration
status 2. Help, version, healthy contributions, and the provider's runtime
registration remain available. Fixing an optional CLI does not require changing
or disabling unrelated transport implementations.

## Public metadata helpers

Import shared helpers from `medre.adapters.metadata`, rather than core-private
implementation modules:

- `versioned_namespace(data, namespace)` accepts a mapping whose
  `schema_version` is a positive integer, excluding booleans.
- `current_namespace(data, namespace, version)` additionally requires the
  supported version.
- `current_native_namespace(data, transport)` and
  `versioned_native_namespace(data, transport)` dispatch through the installed
  provider's reader; absent providers yield `{}`.
- `detect_source_platform(source_adapter, native_data, *, platform_hint=None)`
  uses an explicit hint first, then versioned native evidence in declared
  detection priority, then an exact adapter-ID match, then the longest bounded
  transport-token match. Instance IDs are arbitrary labels, so native evidence
  takes precedence over their spelling.
- `project_source_fields(data, *, source_adapter="", source_transport_id=None,
platform_hint=None)` calls only the selected registered provider's projector.
  An unknown hint returns its `source_platform` with no native interpretation.

## Fake and SDK conformance

A fake may omit `make_event` and remain a delivery target. A smoke source
provides synchronous `make_event(text=..., event_kind=...) -> CanonicalEvent`.
Core always supplies both keywords and explicitly requests `MESSAGE_TEXT`.
Additional positional calling conventions and native default event kinds are
provider conveniences, not requirements imposed on other authors. The returned
event preserves the requested kind and its originating adapter ID.

Run `medre.testing.fakes.assert_fake_contract(spec)` in the provider's SDK-free
test tier. It checks construction with `adapter_id=`, the base contract, and
the optional explicit-keyword event factory. Real inbound simulation methods
remain adapter-specific; core callback injection uses `publish_inbound(event)`.

For providers choosing exact SDK pins, `medre.testing.sdk_contract` offers
`declared_sdk_pins(adapter_distribution, *, extras=())` and
`assert_installed_sdk_pins(adapter_distribution, sdk_distributions, *, extras=())`.
These optional test tools require `packaging` in the test environment and read
installed provider metadata. They work with any distribution name and do not
depend on the curated CI manifest. Core dependencies and inactive test extras
are excluded; active non-core dependencies must use exact version pins.

Core's compatibility manifest only selects reviewed revision pairs and SDK
extras for its own CI. It is not an allowlist for runtime discovery or for these
conformance tools. Generic core tests default to the adapter-free tier; tests
that prove interoperability with installed providers declare compatibility
ownership. Both tiers run in CI, and the full compatibility job includes the
adapter-free tests too.
