# Classify every persistent storage column

A declared registry in the storage package assigns exactly one ownership
class to every persistent table and column: immutable identity, operational
authority, independently owned content, or unclassified. Guard tests prove
the registry covers the real SQLite schema, matches the enforced content
inventory exactly, and reproduces the collector's conservative blocker
decisions without changing any runtime, collector, or schema behavior.
The column-level contract is documented alongside the storage specification
and cross-referenced by the content classification and retention pages.
