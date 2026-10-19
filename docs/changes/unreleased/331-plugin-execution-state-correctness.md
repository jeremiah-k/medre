# Plugin execution state and Python compatibility

- Replace mutable mapping-proxy dataclass defaults with independent factories so
  plugin contract modules import on Python 3.11 as required by MEDRE.
- Keep append-only plugin execution evidence separate from mutable operational
  state: decisions without buffered state writes and failed attempts no longer
  create or advance a plugin state revision.
- Exercise retry, concurrent state writes, and unchanged read-only state
  under the host-owned SQLite transaction contract.
