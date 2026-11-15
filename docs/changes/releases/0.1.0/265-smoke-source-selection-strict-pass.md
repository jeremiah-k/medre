# Explicit smoke source selection and strict delivery pass

- Allow operators to select a configured smoke source with `--source-adapter`.
  Reports now disclose single-source scope and eligible source identifiers.
- Fail the fake bridge smoke when any fanout delivery is unsuccessful rather
  than passing when only one target succeeds.
- Add four-adapter route-source regression tests.
