# Draw configured routes in the terminal and export local graphs

`routes diagram` draws the offline route graph with pinned pure-Python Termaid
by default. `--ascii` provides ASCII output; `--format text` lists per-adapter
connections. Positional bridge YAML paths and private, exclusive `--output`
files simplify inspection and export. Mermaid, DOT and JSON remain text exports;
SVG, PNG and PDF use optional local Graphviz. Incomplete previews refuse image
export. Terminal controls and Mermaid entities remain display data. No native
session starts, and graph output selects no credentials or message bodies.
