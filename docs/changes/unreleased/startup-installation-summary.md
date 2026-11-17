# Startup installation summary

The runtime startup header reports the running installation before adapters
start: the MEDRE core version with its install provenance, the Python
interpreter, and one line per installed provider entry-point transport/distribution pair with
its version and source — an abbreviated recorded Git commit, `index` when direct
URL metadata is absent, an
editable local path, or a direct URL. Community distributions that share the
provider entry point are named explicitly. Provider provenance is read from
installed package metadata without importing adapter SDKs, URLs are redacted
before display (userinfo, queries and fragments), and unreadable metadata
degrades the affected fields or marks inventory incomplete; startup never
blocks on the summary. The labels describe metadata, not qualified source bytes.
