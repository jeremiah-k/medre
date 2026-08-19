# Changed

- Added explicit transport test layers for exact-SDK local integration and soak
  endurance without changing runtime evidence-tier semantics.
- Made LXMF real-session stop/restart release router-owned Reticulum destinations
  and announce handlers so a persistent identity can restart in-process.
- Added deterministic real-SDK local integration for MeshCore TCP and
  process-isolated real RNS/LXMRouter lifecycle integration for LXMF.
- Added opt-in Meshtastic hardware lifecycle soak coverage and marked physical
  radio live tests as hardware evidence.
- Added CI gates for LXMF and MeshCore deterministic local integration plus
  manual local-integration soak jobs.
- Documented failure-oriented realism requirements and current transport gaps.
