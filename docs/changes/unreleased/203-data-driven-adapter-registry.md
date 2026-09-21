# Data-driven built-in adapter registration

- Added a single immutable `medre.adapter_registry` manifest for built-in
  transport assembly metadata. Config loading, env overrides, path validation,
  runtime construction, renderer registration, SDK ownership checks, CLI
  transport discovery/contribution dispatch, support-bundle field
  classification, native-metadata dispatch, and attribution dispatch now
  consume that manifest instead of maintaining parallel transport lists.
- Reworked `AdapterConfigSet` around registry-keyed transport groups while
  preserving existing `config.adapters.<transport>` access for current callers.
  Registered built-ins without a compatibility wrapper use the transport-neutral
  runtime wrapper without adding another field or union member to the root config
  model.
- Moved Matrix-specific route/store preparation behind an adapter-owned runtime
  preparation hook and renderer constructor differences behind adapter-owned
  renderer factories, keeping generic runtime assembly free of transport
  branches.
- Updated architecture enforcement and authoring/spec documentation to make the
  boundary explicit: this is a built-in adapter type registry, not dynamic
  third-party plugin loading.
- Hardened registry assembly after adversarial review: adapter-owned runtime
  preparation now runs as fail-closed preflight for fake and live instances,
  while registered renderer and dependency-probe resolution failures remain
  startup-visible instead of silently falling back or masquerading as an
  uninstalled optional SDK.
- Fixed registry-driven CLI/smoke edge cases for incomplete contributor hooks
  and configurations whose instance key differs from the runtime `adapter_id`;
  legacy transport env-prefix rejection now derives from the registry too.
- Updated Matrix route-preparation and LXMF import-isolation tests to exercise
  the new adapter-owned seams instead of asserting removed RuntimeBuilder
  internals or matching SDK names by substring.
