# Refuse ceiling bypass through inline ingress

A runtime with a declared content ceiling refuses the direct inline ingress
and direct `store_event` paths, which persists with `append` and cannot install an event-level ceiling
in the atomic ingress admission transaction. The supported durable admission
path continues to install and validate the ceiling with its dependency.

Outbox creation refuses a malformed frozen plan deadline rather than treating
the value as an absent bound. The refusal occurs before generation creation.
