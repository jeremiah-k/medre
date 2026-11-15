# Complete the unified relay prefix key for LXMF and promote its pin

The LXMF target adapter now uses the same `relay_prefix` key as the other
configurable renderer targets, completing the cross-adapter unification from
the previous change. The pinned adapter dataclass field is `relay_prefix`
(`str`, default `""`); no old-name alias is accepted. Core tests and helpers,
the `docs/spec` pages that still named `lxmf_relay_prefix`, the adapter-config
JSON schema and its LXMF example, the lab provisioner, and the live-validation
runtime fixture use the unified key. Before upgrading, rename LXMF
`lxmf_relay_prefix` to `relay_prefix`; the matching environment override
becomes `MEDRE_ADAPTER__<TOKEN>__RELAY_PREFIX` instead of
`MEDRE_ADAPTER__<TOKEN>__LXMF_RELAY_PREFIX`. The enclosing transport section
still selects the adapter; there is no runtime compatibility shim.

The LXMF adapter pin is promoted with this change:
`763b9d909d309be11f8385ec52c29db7454aaa92` to
`438a1497849977fb13218199c6a57f5b99afb3db`, which carries the renamed field.
As before, this pre-first-release operator-schema correction intentionally
updates only the `adapter-config.schema.json` digest in `v0.1.0.freeze.json`
(`4573f521c50008be3a153b63fea2884fc4d607a2174729b50bc4f9cbf7255ef6` to
`63bf4b7777c09bb39d40de3bd965b5bbd5d377b97282a783a35bd32943193bdd`); all other
freeze entries are unchanged. Freeze-digest movement requires individual
release-owner signoff before the first tag. Rerun core schema, docs-drift,
cross-adapter and live campaign checks before accepting the final
first-release schema snapshot.
