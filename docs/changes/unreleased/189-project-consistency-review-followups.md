# Project consistency review follow-ups

- Clarify crash/reclaim operator guidance: only eligible non-terminal outbox
  work is reclaimed; terminal rows remain history, and a terminal-outbox /
  non-terminal-receipt mismatch requires receipt-chain investigation.
- Keep `pyproject.toml` as the sole adapter SDK version authority, remove
  duplicated LXMF contract prose, and ensure Matrix E2EE pin validation runs
  before feature-based skip paths.
- Strengthen prerelease contract guards against direct `native_data[...]`
  access and make the storage documentation guard assert schema version `1`.
- Simplify queue-terminal finalization around the already-validated authoritative
  outbox row, removing dead optionality branches without changing lifecycle
  semantics.
- Rename the retained lifecycle terminal-semantics test class/documentation to
  refer to the current specification rather than the removed audit corpus.
