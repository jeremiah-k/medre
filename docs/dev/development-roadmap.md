# Development roadmap

Build operator workflows on the existing platform-neutral routing, persistence,
and provider contracts. Keep adapter implementations and native settings in
their independent distributions. Prioritize usable vertical slices with
explicit evidence limits over adding parallel configuration or routing systems.

| Order | Workstream | Primary outcome |
| --- | --- | --- |
| 1 | Guided setup and configuration | Construct, validate, and understand bridges in one workflow. |
| 2 | Conversation fidelity | Preserve replies, edits, reactions, attachments, and metadata; show honest fallbacks. |
| 3 | Routing explainability and controls | Preview decisions and suppression, identify loops, and configure directional policies. |
| 4 | Long-running operations | Provide health reporting, recovery guidance, diagnostics, and safe state management. |
| 5 | Adapter ecosystem and MMRelay interop | Simplify provider authoring and publish reusable, versioned event-shape examples. |

## First initiative: guided configuration

The first slice is [guided bridge authoring](../ops/guided-configuration.md):
select two configured instances or provider-supplied fake samples, map native
conversation contexts, choose direction and attribution, preview production
route expansion, and save a validated new document. It preserves provider-owned
fields and keeps secrets out of previews and error details.

Subsequent slices can add provider-owned connection forms and context help,
event-specific decision previews, and richer policy controls. Add metadata only
where a real provider needs it; core should not infer platform fields or acquire
package-management responsibilities. Installation remains external through
standard Python packaging and the operator's chosen tooling.

## Engineering gates

- Keep configuration authoring and preview on the same models and compiler as
  runtime startup; avoid a second interpretation of routing rules.
- Exercise community providers and core-only installations, not only first-party
  names or namespace layouts.
- Preserve raw configuration values while validating effective environment
  overrides; do not persist an environment's secrets into generated YAML.
- Distinguish offline configuration checks, fake pipeline evidence, and native
  service/device qualification. A successful preview is not delivery proof.
- Use focused regression tests for changes and CI for broader compatibility.
  Keep private campaigns and audit artifacts outside the repository.
