# Single-host SQLite measurement

`python scripts/measure_sqlite_recovery.py /tmp/medre-sqlite-sample --events 250`
creates a **disposable** SQLite database of bounded synthetic canonical
events, measures append latency, closes and reopens the database, and verifies
that every event survives. The destination must not already contain the
measurement database. The tool reports median and p95 append time, throughput,
file footprint and reopen time (storage initialization only, excluding retained
count verification and closing) without enforcing environment-specific latency
thresholds or changing production configuration.

This is an initial reproducible baseline, not a native-transport load test or a
capacity promise. It does not measure the scheduler, retries, physical radios,
content-addressed attachments, actual event fanout or adapter profile recovery.
Run with progressively larger counts and record filesystem, device, Python and
SQLite versions before choosing any index, WAL or retention optimization.
