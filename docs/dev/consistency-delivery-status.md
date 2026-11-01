# Consistency, recovery and delivery status

This is an implementation and evidence summary, not a fixed development plan.
The [native evidence index](native-evidence-index.json) records exact campaign
sources, substrates, and limits. The frozen 42-directed-pair, 168-receipt baseline
remains historical regression evidence rather than a target to repeat for each
change. Compatibility pins select qualified revisions; newer adapter heads do
not silently replace those selections.

| Capability                        | Implemented contract                                                                                                                                                                                               | Qualification boundary                                                                                                                                                                       |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical content and attribution | Shared text selection, scoped lineage, and provider-specific native context.                                                                                                                                       | Display attribution and retained source claims do not grant native identity authority.                                                                                                       |
| Multipart radio handoff           | Exact persisted plans, paced owned sends, one-use packet grants, and attempt-bound progress on Meshtastic and MeshCore.                                                                                            | Local packet acceptance does not prove recipient delivery.                                                                                                                                   |
| Radio continuation                | Configured retries reserve a fresh claim and continue only a proven-unsent suffix, preserving each accepted packet's original attempt.                                                                             | Recorded physical process-interruption cases passed on both transports; host power loss, firmware reset, long outages, and extended duplicate windows remain unqualified.                    |
| Packet observations               | Transactional admission against original accepted facts and a read-only whole-plan projection. Meshtastic normalizes routing callbacks conservatively.                                                             | The Meshtastic classifier does not emit delivered observations. MeshCore channel messages do not gain a fabricated packet-ACK capability.                                                    |
| Native health                     | Seven provider implementations publish settled native transitions through adapter-bound lifetime-fenced reports. Startup history remains immutable.                                                                | Healthy describes each provider's local/native scope, not universal peer reachability or delivery.                                                                                           |
| SimpleX ownership and recipients  | Linux inherited profile exclusion survives the recorded bridge-process death while a native worker remains. Independent group text observations are recorded separately.                                           | Worker fencing is not a recipient receipt; direct-contact and worker-restart recipient qualification remain separate.                                                                        |
| Stopped-state restoration         | Verified selected-state capture, committed SQLite WAL backup, and restore into an absent destination. A seven-file core/SimpleX restore preserved native history and reused an accepted handoff without resending. | Checksums establish captured-byte integrity, not provider completeness, exclusive activation, or power-loss durability. Provider declarations require their own layout/export qualification. |
| SQLite operating limits           | A bounded synthetic append/reopen measurement utility and recorded local baseline.                                                                                                                                 | This is not a mixed-workload capacity guarantee; fanout, backlogs, retention, and storage-pressure behavior need measurements.                                                               |

## Explicit tail recovery ownership

The storage and synchronous service APIs transfer a committed accepted prefix
into a newer independently reserved claim. Each packet retains its original
accepting attempt. Continuation sends only the persisted unsent suffix and keeps
the weakest inherited or fresh local confirmation. The retry worker selects the
latest earlier checkpoint under its fresh claim. Changed native semantics and
unknown or submitting work remain held; retained fingerprints are required for
plain-channel continuation.

The recorded native recovery run covers process interruption with physical peers,
including two unsent packets continued after one accepted packet and zero replay
of uncertain submissions. Its eight-second peer-observation windows do not prove
an absence of arbitrarily late duplicates. The evidence index, rather than a
moving adapter checkout, is the authority for those exact observations.
