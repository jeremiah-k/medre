# Strict Source Typing

Cover the complete MEDRE core source tree and all seven adapter repositories
with the configured strict-mypy gate. Use structural protocols, typed receipt
fields, and exact SQLite mixin signatures while preserving runtime contracts.
Run core's strict check in the required quality job. Optional transport SDK
import exemptions remain a separate typing surface.

Architecture import analysis evaluates lambda defaults without treating deferred
lambda bodies as import-time calls, and retains immediately invoked bodies.
