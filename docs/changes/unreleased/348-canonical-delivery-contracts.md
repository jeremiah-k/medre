Canonical styled parsing and text rendering now share one text-selection contract:
styled bodies remain authoritative, while unstyled messages retain legacy text
aliases. Native text paths reject malformed values. Pure radio progress snapshots
bind to an independently held attempt and plan fingerprint, require ordered local
handoffs, and restore interrupted submissions as unknown. The new progress schema
documents the persisted shape; storage transactions and native multipart dispatch
remain separate integration work and are not enabled by this change.
The compatibility catalog also adopts the merged, hardened Chatmail revision.
Core metadata and lockfile now declare 0.1.1 so adapters can require this contract
without claiming compatibility with earlier core packages lacking the selector.
