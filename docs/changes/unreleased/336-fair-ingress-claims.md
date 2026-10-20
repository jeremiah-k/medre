# Fair Durable Ingress Claims

Prioritize least-recently updated ingress work, with creation time and event ID
as deterministic tie-breakers. Released or operationally deferred rows rotate
behind untouched pending rows instead of monopolizing the oldest claim slot.
Operational deferral preserves the processing-failure budget, and the worker
pauses between deferred cycles.
