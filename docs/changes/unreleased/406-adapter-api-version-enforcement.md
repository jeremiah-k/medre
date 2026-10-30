# Require explicit adapter API versions

Every provider declares the literal integer `AdapterSpec.api_version=1`.
SDK-free discovery refuses omitted, malformed, boolean and incompatible values
before loading native implementations, while preserving healthy providers.
The API major version is independent of package tags and SQLite schema versions;
matching it does not qualify native capabilities or package minimum features.
The compatibility manifest selects exact merged provider commits.
