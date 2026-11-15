# Share bounded relay attribution across adapters

Expose the existing Matrix relay context builder and validator in core so other
adapters can carry the same sender claims, origin labels and scoped references.
Publish its schema and support omitting relation hints for text fallback.
Expose shared message-body extraction so adapters preserve authoritative styled
bodies while retaining legacy text aliases.
