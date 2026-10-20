# Stage-Neutral Plugin Kind Contracts

Make plugin event subscriptions and emission limits stage-neutral with
`input_kinds` and `output_kinds`. A declared input filter applies to
all stages; transforms require an explicit input list. Every plugin
requesting `emit_events` declares allowed output kinds, including enrichment
plugins. The host rejects undeclared output kinds and does not invoke
plugins for unrelated inputs.

Document the current canonical processor boundary separately from
potential adapter-admin, outbound-intent, query, and web services.
No runtime services for those future use cases are introduced here.

Distinguish kind subscriptions from resource authorization and document extension
choices for translation, rendering, dashboards, and curated adapter operations.
Canonical stages do not provide timers or delivery-feedback subscriptions.
