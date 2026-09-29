# Expose LXMF SDK-boundary delivery counts for ingest attribution

- The LXMF session counts every `LXMRouter` delivery-callback invocation
  in its diagnostics (`deliveries_received`), and the adapter surfaces
  the count alongside its existing inbound evidence. The pinned router
  proves each inbound packet to the sender before the app-side callback
  runs and consumes every later drop silently, so a sender-side
  DELIVERED state never implied admission; the counter separates "the
  router never handed the message over" from normalisation, classifier,
  dedup, and publish losses.
- The radio-matrix harness attaches this evidence to its LXMF
  fan-out and admission failure messages, naming the stage that lost
  the message instead of only reporting the missing event.
