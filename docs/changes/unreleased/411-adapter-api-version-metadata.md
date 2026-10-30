# SDK-free adapter API version metadata

`AdapterSpec.api_version` declares the major version of the SDK-free adapter
interface implemented by a provider. `ADAPTER_API_VERSION` identifies the major
version supported by core (initially `1`). The version is independent of the
Python distribution version and the SQLite schema version. The prerequisite
permits an omitted declaration as unknown (`None`) only until all provider PRs
land; a subsequent core PR makes the declaration mandatory at discovery.
Explicit non-integer or unsupported API versions are rejected during SDK-free
discovery; booleans are not API versions.
