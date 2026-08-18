# Recoverable durable ingress worker

- Add a recoverable worker for pending ingress work with lease-based crash reclaim.
- Add a pipeline path that routes already-admitted events without re-storing them.
- Wire protocol-provenance admission into adapter runtime context without changing
  existing adapter ingress behavior.
