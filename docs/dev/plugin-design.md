# Choosing a Plugin Extension

Use this guide to choose where an extension belongs before adding host authority.
The [plugin specification](../spec/plugins.md) defines the implemented contract;
its status table distinguishes explicit execution from operator activation.
Installed plugins are trusted Python code. Capability checks describe supported
host operations, not a process sandbox.

## Match the Extension to Its Owner

| Extension                                             | Fit with the canonical stage host                                                                            | Additional requirements                                                                                                                                                                                |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Text classification, local label lookup, statistics   | Enrich with declared `plugin.custom` output; retain the original.                                            | Explicit routes to deliver observations; bounded state for counters.                                                                                                                                   |
| Transliteration or local translation                  | Supersede with a declared message kind when all downstream destinations should receive the transformed text. | Preserve structured content deliberately; schema versions belong to the proposed payload.                                                                                                              |
| Content filtering                                     | Return a policy denial with a reason.                                                                        | Kind subscriptions filter events, not destination permissions. Destination-specific policy belongs in route planning.                                                                                  |
| Telemetry threshold alerts                            | Derive an observation when an admitted report crosses a threshold.                                           | Explicit routes and rendering for alerts. Inferring silence or issuing periodic summaries needs a durable timer.                                                                                       |
| Commands such as ping or weather                      | A canonical proposal can describe a command result, but routing still follows the triggering source context. | Bot identity, reply relations, authenticated issuer and destination selection are separate decisions. A remote weather lookup also needs defined external-I/O behavior.                                |
| Curated mtjk commands                                 | Adapter-owned operations, rather than canonical derivation.                                                  | A typed, limited vocabulary, authenticated issuer, device/resource grants, operation-specific confirmation and retry rules. This includes permitted reads and other commands, not only administration. |
| Dashboards and audit export                           | Consumers of a host read-only query/stream interface.                                                        | Pagination, authorization, redaction, retention and backpressure. No such plugin context service is implemented.                                                                                       |
| Webhooks, push notifications, scheduled announcements | Outbound intents or scheduled work.                                                                          | Explicit destination authority and durable effect ownership; do not implement a parallel transport inside a stage.                                                                                     |

Message appearance at one destination is a rendering concern. For example, a
radio-only transliteration or shorter label should not replace the canonical
message for Matrix and every other destination. Inspect the existing rendering
pipeline before introducing another transform mechanism.

An enrichment is additive: deriving a cleaned-up or translated message also
leaves the original eligible for routing. Use a transform with `Supersede` when
replacement is intended. The original remains stored, and descendants retain its
source context. Derivation does not mint a new speaker identity or prove that
an issuer may perform a device command.

Subscriptions select event kinds; they are not channel, actor, device or
per-destination grants. The host receives admitted ingress and forward
continuations, not a global subscription to every event later written to
storage. In particular, a stage is not a delivery-feedback listener. An alert
about silence cannot run merely because no event arrived.

## Grow the Interfaces from Concrete Uses

Keep canonical derivation, outbound requests, adapter operations and read-only
queries separate. Share configured instance identity, package management and
execution evidence across them where useful. Avoid an unrestricted `execute`
method or access to live adapter clients.

Choose a narrow reference use for each additional interface. A read-only
conversation/evidence view can establish dashboard requirements. A small curated
mtjk operation can establish resource authorization and confirmation requirements.
A translation provider can establish external lookup deadlines, privacy,
idempotency and recovery requirements. None requires committing to a general bot
framework or workflow scheduler.

For remote translation, make the distinction between host durability and external
behavior explicit. A plugin decision can be committed once while an interrupted
lookup is attempted more than once. Decide what text may leave the bridge, how
requests are identified, what is retained, and whether failure retains the
original text or defers a required transformation. The stage host supplies a
stable invocation key and bounded attempts; it does not supply a durable network
job queue, exactly-once billing, or permission to send as another user.

A useful reference plugin proves the interface and its failure behavior. It does
not need to be a bundled operator feature. The SDK-free text-statistics fixture
already serves that role for canonical derivation.

A plugin distribution can also ship a frontend or a platform-specific bot. That
packaging choice does not put a web server or a second transport runtime inside
the canonical stage host.
