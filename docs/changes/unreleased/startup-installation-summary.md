# Startup installation summary

The runtime startup header reports the running installation before adapters
start: the MEDRE core version with its install provenance, the Python
interpreter, and one line per installed provider distribution with its version
and source — a git commit for Git checkouts, `index` for registry releases, an
editable local path, or a direct URL. Community distributions that share the
provider entry point are named explicitly. Provider provenance is read from
installed package metadata without importing adapter SDKs, URLs are redacted
before display, and unreadable metadata degrades that line only; startup never
blocks on the summary.
