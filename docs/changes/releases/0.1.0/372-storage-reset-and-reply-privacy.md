# Clarify disposable storage reset and reply-content boundaries

Distinguish disposable test storage from retained pending or uncertain delivery
work. Document bounded read-only SQL inspection for an incompatible store and
make backup retention an explicit choice.

Explain that native reply correlation uses identifiers while quoted fallbacks
can need retained message text. Canonical payloads, fallback quotes, attachment
bytes, provider stores and backups have distinct persistence boundaries;
resetting a core database is not a content-erasure or native-ownership grant.
