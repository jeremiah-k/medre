# Promote the Chatmail adapter to the merged dependency-pin head

Pin the Chatmail adapter to the merged revision carrying lockstep
`deltachat-rpc-client`/`deltachat-rpc-server` 2.68.0 pins with the
dependency-license disclosure updated to match. The adapter enforces
client/server version agreement, so the client-only renovate proposal was
completed to a matching server bump before qualification. Runtime behavior
is unchanged: the full SDK and fake suites pass against 2.68.0.
