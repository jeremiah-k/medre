# Bounded source-native facts in shared relay context

The v1 shared bridge context can project one explicitly public, source-owned
native namespace with at most twelve bounded scalar facts. It does not copy raw
native metadata, grant identity or relation authority, or override canonical
text. A renderer may receive a source-supplied generic attribution snapshot
when its embedding consumer owns the original transport session. The canonical
JSON/YAML event model is unchanged; the pre-first-tag context schema and its
freeze digest are deliberately updated.

Normalize finite integral JSON numbers to bounded integers in public source
facts, matching the published schema. Keep native namespaces with missing,
boolean, or unsupported schema versions opaque.
