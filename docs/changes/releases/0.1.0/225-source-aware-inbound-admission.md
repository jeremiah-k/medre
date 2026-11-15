# Source-aware inbound admission fairness

- Make the bounded inbound-admission gate source-aware without reducing normal
  throughput: active execution slots remain runtime-global and work-conserving,
  while the pending overload queue is partitioned into equal fair shares across
  built adapters and drained round-robin by adapter ID.
- Bind every runtime adapter context's `publish_inbound` / `admit_inbound` seam
  to its adapter ID and expose per-source current, waiting, fair-share limit,
  rejection, timeout, and oldest-wait diagnostics in the capacity snapshot.
- Count inbound admission rejection in the runtime-wide `capacity_rejections`
  accounting counter as well as the gate-local counters.
- Preserve cursor ownership under overload: rejection on the cursor-aware
  `admit_inbound` seam becomes `DurableIngressDeferredError` with the stable
  `inbound_admission_capacity` reason, so Matrix/nio leaves the native event
  pending instead of consuming it. Ordinary callback-only `publish_inbound`
  keeps the existing counted-loss `InboundAdmissionRejected` contract.
- Keep reduced/test runtimes source-compatible: zero-argument capacity
  controllers still work when no adapter ID is bound to the seam.
