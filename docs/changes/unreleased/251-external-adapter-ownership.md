# Complete external adapter ownership

Move transport configs, runtime wrappers, validation errors, fakes, SDK pins,
and single-transport tests to the independent adapter distributions. Core keeps
only generic contracts/orchestration and its development extra. Remove the
runtime-wrapper import facade and the registry's built-in naming/order rules.

Make runtime config and route destination schemas accept community providers.
Native destination kinds now register pure `DestinationSpec` validators;
Matrix room, MeshCore contact, and LXMF hash validation live with their adapters.
Generic channel validation stays in core. Native addressing must be supported
by the configured target; mismatches fail before adapter construction.
Adapter `enabled` values must be actual booleans.

Separate adapter-free core checks, independently owned adapter suites, and the
cross-adapter compatibility set. Generate CI jobs from a manifest and snapshot
moving adapter refs to full commit SHAs once per run. Core independence checks
cover Linux, macOS, and Windows. The installed-wheel proof defaults to core;
external wheels can additionally prove the sample bridge without source or SDKs.

Remove the unused radio trait/category. Recovery duplicate-send guidance uses
recorded outbound native references and unresolved retryable failures, without
inferring platform behavior from adapter identifiers or package labels.

Smoke source selection follows configured route sources without preferring a
particular platform. Lifecycle drills use installed fake providers rather than
fixed first-party transport names.
