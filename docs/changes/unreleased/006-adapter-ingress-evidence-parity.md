# Adapter Ingress Evidence Parity

Harden post-stop ingress behavior and fill LXMF diagnostics evidence gaps.

## Changed

- `src/medre/adapters/*`: post-stop inbound simulation and Matrix room callbacks now drop lifecycle-stale events instead of publishing through retained contexts.
- `src/medre/adapters/lxmf/adapter.py`: exposes LXMF session diagnostics fields and message-level ingress counters, including duplicate suppression and published counts.
- `src/medre/adapters/lxmf/adapter.py`: drops terminal delivery-state callbacks after adapter stop.

## Added

- `tests/test_adapter_post_stop_ingress.py`: cross-adapter post-stop ingress and LXMF delivery-state callback coverage.
- `tests/test_lxmf_diagnostics_parity.py`: LXMF diagnostics field and ingress counter coverage.
- `tests/test_matrix_boundaries.py`: Matrix rate-limit retry coverage proving stable `tx_id` reuse for the same rendered result.
