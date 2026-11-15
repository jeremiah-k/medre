# Analyze retained native campaign evidence offline

The campaign analysis command checks directed-pair coverage, independent receipt
counts, durable source and delivery provenance, and native owner cleanup without
connecting to services or printing private message content. Reports retain the
configured transport, source channel, and independent native sender/conversation
expectations so incorrect outbox channels or wrong-peer receipts cannot pass.
Source markers must be nonempty, unique, and nonoverlapping; declared full coverage
must include all seven transports. Native and canonical text must contain their
source nonce exactly once; non-text payloads and missing cleanup evidence cannot
qualify. Incomplete or inconsistent reports exit
nonzero. Public guidance describes fixture preparation,
source capture, failure retention, and separate connectivity/recovery tiers.
