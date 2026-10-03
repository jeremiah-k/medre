# Matrix crypto-store lifecycle cleanup

Matrix session shutdown and partial-start cleanup close the crypto database
after nio provider close settles. The pinned provider closes HTTP and drains
recovery callbacks but leaves this database connection open. E2EE bootstrap
uses the same cleanup boundary.

Cleanup stays attached to the bounded provider-close task, including when it
outlives the caller's stop deadline or the shutdown caller is cancelled.
Shutdown retries leave that task's protected recovery drain intact. Persisted
keys and device identity remain available for restart. Store-close failures
are logged.
