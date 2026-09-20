# 192: Route LXMF deliveries to the recipient named by the route

LXMF: routed deliveries now address their recipient. The renderer carried a
hard-coded empty `destination_hash` (documented placeholder), so every routed
egress through the real LXMF adapter failed permanently with "cannot recall
identity". The renderer now resolves `destination_hash` from the route's
`dest_channel` (the recipient's 32-hex LXMF delivery destination hash, per
the routing-delivery `"lxmf_destination"` contract). `LxmfConfig` also gains
`reticulum_config_dir`, making the session's documented isolated-Reticulum
seam (one RNodeInterface, `share_instance = No`) reachable from runtime
configuration instead of only via programmatic injection. The adapter JSON
schema and its example now cover the field (it was missing from the
published contract despite being accepted by the loader).
