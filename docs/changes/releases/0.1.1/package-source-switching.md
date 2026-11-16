# Explicit package source switching

`medre sources show` separates configured sources, uv's saved request and
installed provenance. `medre sources switch` selects index releases, Git
repositories/revisions or local checkouts for core and installed adapters,
with a read-only dry run. Source changes preserve extras and unrelated requests;
uv retains resolution and receipt ownership. Local editable requests survive
supported replay. Source catalogs and persisted bridge data are unchanged.

New first-party adapter requests default to index releases (PyPI under uv's
default configuration). Existing catalogs and installed requests retain their
choices. Inline adapter version constraints select index versions explicitly.
The legacy `adapters reset` command continues to select upstream Git main.
