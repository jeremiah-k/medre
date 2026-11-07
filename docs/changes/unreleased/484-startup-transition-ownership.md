# Keep process lifecycle transitions aligned with startup ownership

Tighten the process lifecycle guard: while `MedreApp.start()` is in flight,
`MedreApp.stop()` refuses independent shutdown and only the startup task may
own cancellation/rollback. The transition graph now rejects `STARTING ->
STOPPING` instead of advertising an edge that no public runtime operation can
legally take. Preserve all other transitions and the runtime event contract.
Also clarify in the `RuntimeState` docstring that only nonterminal states may
enter `FAILED`, as requested in the PR's Sourcery review.
