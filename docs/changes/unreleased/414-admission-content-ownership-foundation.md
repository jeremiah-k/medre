# Admit content ownership at ingress and dispatch

Separate canonical string bodies and relation excerpts from event envelopes in strict prerelease SQLite state; atomically admit ingress and outbox content owners; add trusted owner-bound reads and a native dispatch-start fence. No retention default, automatic collection, erasure guarantee, or complete general-read mediation is implied. See `docs/spec/content-runtime-authority.md` for remaining enforcement gates.
