# Adopt the merged trunk lint-refresh revisions

Pin every adapter to the merged revision carrying the shared trunk
toolchain refresh: trunk lint pins bumped in lockstep with the CI quality
job (ruff 0.17.3, black 26.10.1, checkov 3.4.0, renovate 44.199.0,
markdownlint 0.50.0, trufflehog 3.99.7) and `trunk check --all` clean on
each branch. pinact stays held at the plugin-known-good 4.0.0 in every
repo because the trunk plugin wrapper still passes single-dash v4 flags
that pinact v5 rejects. Adapter runtime behavior is unchanged; the
revisions also carry mechanical markdown heading and schema formatting
fixes. mypy remains intentionally absent from trunk linting in all repos.
