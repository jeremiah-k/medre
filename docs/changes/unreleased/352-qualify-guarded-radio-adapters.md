# Qualify merged MeshCore handoffs and define queued packet ownership

The compatibility snapshot now adopts the exact merged MeshCore #13 revision,
qualified by its fake and installed-SDK CI against merged core main. The audit
status separates that result from pending physical recovery evidence.

The radio contract now describes synchronous packet waiters through an existing
paced queue: core retains its current claim, cancellation fences pending native
work, and individual packets do not emit deferred parent completion. The
coordinated Meshtastic candidate implements this boundary while ordinary sends
retain their existing deferred behavior. Native ACK aggregation and automatic
partial-message restart resumption remain outside this qualification.

Package versions and storage shape/marker are unchanged.

The MeshCore adapter configuration schema and example also declare the
qualified `max_text_parts` setting with its 1–128 bound and default of 1.
