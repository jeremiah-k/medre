# Route-aware failure drill target selection

Adapter failure drills (`adapter_permanent_failure`,
`adapter_transient_failure`) now patch the first sorted non-source adapter
that is a delivery target of an enabled route leg from the chosen source,
instead of the first sorted non-source adapter. Bidirectional routes count
through both of their expanded legs. When no enabled route delivers from
the chosen source to any other adapter, the drill fails early with a
`no delivery target for source <id>` reason instead of patching an adapter
the injected event can never reach.

This fixes false drill failures on topologies where the alphabetically-first
route source does not route to the alphabetically-first other adapter (for
example, an LXMF source whose only route targets MeshCore): the drill
previously patched Matrix, observed no outcome for it, and reported failure.
Configs where the old first-non-source heuristic already picked a routed
target behave identically.
