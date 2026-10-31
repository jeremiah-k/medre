# Classify incoming relation excerpts for bounded collection

Per-event release assessment now includes quote excerpts retained by other
canonical events as an explicit blocker. Only separately persisted canonical
body and same-event excerpt rows are candidates for the first collection
subset; no native/evidence/attachment bytes are implicitly collectible.

Self-referential excerpts are not external copies; incoming native-reference-only
quotes are counted as independent blockers. Long persisted event identifiers can
be assessed without an unrelated preview length limit.
