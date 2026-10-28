# Previewing route connections

`routes diagram` is an **offline configuration preview**. It never starts an
adapter, joins a Matrix room or probes radio health. Nodes indicate configured
potential sources and destinations; they do **not** indicate live eligibility,
permissions, delivery success or a complete list of joined rooms.

```bash
medre config use ~/medre/medre-4way-meshcore.yaml
medre routes diagram                                   # Unicode terminal graph (default)
medre routes diagram --ascii                           # plain ASCII for older terminals
medre routes diagram --view adapters                   # compact terminal overview
medre routes diagram --format text                     # per-adapter route list
medre routes diagram ~/medre/medre-4way-meshcore.yaml   # positional shorthand
medre routes diagram --format mermaid --output bridge.mmd
medre routes diagram --format svg --output bridge.svg   # local Graphviz dot
medre routes diagram --format png --view adapters --output overview.png
medre routes diagram --format json --output routes.json
medre config current                                    # show precedence
medre config unset                                      # forget the selection
```

The default terminal view **draws a Unicode graph in terminal cells** using
[Termaid](https://github.com/fasouto/termaid), included in MEDRE's base dependencies.
It works offline in local terminals and SSH sessions; it needs no browser,
external executable, terminal image protocol, or network renderer. The terminal
layout flows top-to-bottom to make connected groups more readable at ordinary
terminal widths. Use `--ascii` for terminals without box-drawing glyphs, and
`--view adapters` for a smaller overview that hides individual channels.
`--format text` instead lists each adapter's incoming and outgoing legs with
source/destination contexts. These views come from the *same* offline route
plan and make no claims about live connectivity. The **contexts** Mermaid view groups route-connected
endpoint pairs and makes context remaps (`3 → 2`, etc.) visible. Grouping uses
*topological connected components*, not room names or message histories;
wildcard source filters are shown as `*` nodes rather than expanded into
hypothetical conversations. Connected components describe configured edges,
not equivalence between conversations or native addressing schemes. The
**adapters** view aggregates routes into a compact network overview, at the cost
of hiding individual channels. Matching reverse route groups share one bidirectional
edge; different directional groups retain separate arrows.

`--format mermaid` emits the Mermaid source without changing its layout.
`--format svg`, `png`, or `pdf` renders a **Graphviz layout** locally using the
optional `dot` executable (no network service and no adapter I/O). This is an alternative layout of the same configured topology, not
Mermaid rendering. To render the exact Mermaid source, use a locally installed
`mmdc` with the
[Mermaid flowchart syntax](https://mermaid.js.org/syntax/flowchart.html).
Binary images require `--output`; the CLI will not send binary data to the
terminal. All `--output` files are new and mode `0600` on POSIX (Windows uses directory ACLs); existing files are not
overwritten. Incomplete previews cannot be exported as images. Graphviz is optional; Termaid is required by MEDRE for its default graph. The
terminal renderer supports both Unicode and ASCII text. It is a static view,
not an interactive TUI. Terminal layout is limited to 200 nodes and 500 context connections; oversized
previews fail with a text/Mermaid fallback hint. In large graphs, try `--view adapters`,
`--format text`, or the SVG export to avoid terminal width limits.

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
