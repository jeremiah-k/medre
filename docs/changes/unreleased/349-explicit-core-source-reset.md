# Recover a retired core source explicitly

`medre adapters reset --include-core` restores core and selected installed
first-party adapters to main while preserving core feature extras and unrelated
requests. Plain adapter reset retains core; if uv fails, it now prints the
explicit recovery path and preserves the failure status without retrying.

Source catalogs, package versions, runtime configuration and storage are unchanged.
