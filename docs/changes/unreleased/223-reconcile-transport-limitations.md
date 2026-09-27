# Reconcile transport limitations with current capabilities

- Correct the Matrix limitations entry: outbound replies, threads,
  reactions, edits, and deletes are native, and outbound attachments
  relay through the durable bounded media transfer; the appendix still
  claimed edits, deletes, and attachments were unsupported after the
  relation and attachment work landed. The room-key backup gap remains
  documented.
- State the inbound admission bound alongside the delivery bound in the
  core capacity limitation, including counted rejection past the
  admission timeout.
- Verified against the current tree: the fake-transport smoke run
  passes with all deliveries accounted for, and the Matrix capability
  declaration is the authority for the outbound surface.
