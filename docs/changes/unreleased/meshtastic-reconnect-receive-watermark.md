# Meshtastic reconnect receive watermark

The selected Meshtastic provider closes the reconnect backlog gap left by the
startup packet drain (#510): after a transparent reconnect, a radio can empty
its FROMRADIO queue of pre-reconnect packets, and the original startup
baselines let that queued traffic relay as fresh. The provider now records a
receive-ready watermark per connection generation. The `rxTime` cutoff is
judged against the packet's own generation's watermark, and a fixed
five-second, generation-scoped window drops packets that arrive without a
usable `rxTime` right after a reconnect; provably fresh traffic relays
immediately.

No new configuration key: the existing `startup_packet_drain_seconds: 0`
opt-out disables the reconnect window along with the startup drain, and
`startup_backlog_suppress_seconds` remains the `rxTime` cutoff tolerance.
The provider PR lands first; this documents the behavior for the core spec
(transport profile and the startup-backlog row of the transport-limitations
appendix). The adapter-compatibility pin bump rides the usual companion PR.
