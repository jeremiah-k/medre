# Adapter limitations provenance refresh

Re-verify the known-limitations and transport-limitations indexes against the
seven transport pins recorded in the adapter compatibility manifest, and
record those inspected commits as the indexes' checked basis, naming each
provider's native SDK package (`mindroom-nio[e2e]`, `mtjk`, `meshcore`,
`lxmf` with `rns`, `slixmpp`, `deltachat-rpc-client` with
`deltachat-rpc-server`, `simplex-chat`) while leaving its exact pin declared
only in the provider's package metadata.

Correct Matrix claims to the pinned behavior: startup validates a persisted
token via `whoami()` whenever no `device_id` is configured (plaintext startup
always does), cross-signing reset happens through the authenticated
`auth login --reset-cross-signing` command (OAuth or fresh password), and the
durable store's POSIX file-lease requirement leaves Windows production
sessions unsupported. Correct Meshtastic claims with the pinned send path's
`wantAck=False` evidence rules and an explicit statement that node-number
stability across rejoin is not verifiable from the provider repositories.
Correct MeshCore claims with the provider-owned `meshcore` SDK pin, the
deterministic local real-SDK TCP integration tier, and the 160-byte firmware
group-text budget. Reframe LXMF claims around the owned native worker
process: retries are bounded only before the native router call, uncertain
handoffs require operator reconciliation, terminal-fact contexts expire after
15 minutes without implying non-delivery, Windows descendant ownership is
unqualified, and RNS-internal detach/reconnect behavior is declared
unverifiable from the provider repositories.

Add transport sections for XMPP, Chatmail, and SimpleX Chat with limitations
taken from each pinned provider's README, specification, and code, and extend
the fire-and-forget, startup-backlog, and protocol-neutrality tables to all
seven transports. Record the LXMF relation harness as part of the provider's
local-integration tier that core runs against the pinned revision, with its
loopback gate pass kept as dated evidence, and record the LXMF degraded
health state alongside healthy/failed/unknown.
