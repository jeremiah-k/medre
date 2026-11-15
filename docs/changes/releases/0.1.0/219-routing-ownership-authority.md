# Routing ownership authority and truthful preflight

- Promote route `ownership` (`shared` / `exclusive`) from dormant core metadata to validated configuration and environment overrides, preserving it through standard, bidirectional, and `context_map` expansion.
- Reject overlapping enabled exclusive route source domains during runtime startup before adapter-build degradation, and expose the same deterministic conflicts through `medre routes validate` and `medre routes plan` after applying the same environment overrides as runtime. Disabled routes and shared routes remain overlap-permissive.
- Reconcile the normative routing specification with the implemented route model: empty event-kind filters are wildcard matches, there is no generic `filters` mapping, broadcast is the only fanout strategy, and dynamic route reload remains unsupported.
- Surface ownership in route topology/list/plan output and configuration schemas/sample documentation so preflight and runtime share one route authority.
