# Previewing route connections

`routes diagram` is an **offline configuration preview**. It never starts an
adapter, joins a Matrix room or probes radio health. Nodes indicate configured
potential sources and destinations; they do **not** indicate live eligibility,
permissions, delivery success or a complete list of joined rooms.

```bash
medre routes diagram --config bridge.yaml
medre routes diagram --config bridge.yaml --format mermaid > bridge.mmd
medre routes diagram --config bridge.yaml --format mermaid --view adapters > adapters.mmd
medre routes diagram --config bridge.yaml --format json > routes.json
```

The default terminal view lists each adapter's incoming and outgoing legs with
source/destination contexts. The **contexts** Mermaid view groups route-connected
endpoint pairs and makes context remaps (`3 → 2`, etc.) visible. Grouping uses
*topological connected components*, not room names or message histories;
wildcard source filters are shown as `*` nodes rather than expanded into
hypothetical conversations. Connected components describe configured edges,
not equivalence between conversations or native addressing schemes. The
**adapters** view aggregates routes into a compact network overview, at the cost of hiding
individual channels. Matching reverse route groups share one bidirectional
edge; different directional groups retain separate arrows. Mermaid is emitted
as source, not rendered inside the terminal. For SVG/PNG, use an external local Mermaid renderer such as `mmdc` with the
[Mermaid flowchart syntax](https://mermaid.js.org/syntax/flowchart.html).

Room and long-address identifiers are abbreviated for readability by default.
**Abbreviation is not redaction:** short names remain visible, and distinct
identifiers can share a prefix. Structured destinations abbreviate the identifier
while retaining its addressing kind. Colliding text/Mermaid labels include a node
index. Every JSON connection endpoint has an `id` matching a node in the
Mermaid **contexts** view; the **adapters** overview has separate adapter IDs.
Use the endpoint ID, rather than its display fields, to identify endpoint nodes.
IDs are deterministic
within a configuration snapshot and unchanged by `--full-context`; they are not
native identities or stable IDs across configuration edits.

`--full-context` reveals exact room IDs and native addresses. All output can
contain private route names, adapter names and topology; review it before
sharing. The command selects no credentials or message bodies for graph output.
Text output escapes configured terminal controls instead of executing them.
Mermaid exports mark incomplete previews and the CLI exits nonzero with a
stderr explanation when the plan contains errors. For the full plan, including
skipped, invalid and disabled definitions, use `medre routes plan --json`.

Interactive terminal viewers, if they are built, should consume the
machine-readable JSON graph and use a selectable per-adapter list or tree with
optional context-specific details. Rendering a many-edge ASCII graph in a
narrow terminal becomes ambiguous.
