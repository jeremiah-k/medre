# LXMF Announce Interval Configuration

Add configurable periodic LXMF announce interval for mesh path discovery.

## Changed

- `src/medre/config/adapters/lxmf.py`: Added `announce_interval_seconds`
  field (float, default `600.0`, i.e. 10 minutes). `0` disables periodic
  announces. Validation rejects non-finite and negative values.
- `docs/spec/transport-profiles/lxmf.md`: Documented
  `announce_interval_seconds` config field and semantics.
- `docs/schemas/adapter-config.schema.json`: Added
  `announce_interval_seconds` (number, default `600.0`) to LxmfConfig.

## Configuration

- `announce_interval_seconds`: New optional float field on LXMF adapter
  config. Default `600.0` (10 minutes). `0` disables periodic announces.
  Only used in non-fake connection modes — fake mode never creates
  network-visible announces.
