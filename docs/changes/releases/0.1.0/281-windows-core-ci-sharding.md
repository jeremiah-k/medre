# Deterministic Windows core CI shards and reliable subprocess capture

Partition the adapter-free Windows core suite into eight complete-module test
shards while retaining single-job Linux/macOS coverage. The shard assignment
is deterministic and balanced using actual pytest collection counts. Every
collected test runs exactly once across the Windows jobs; missing or invalid
shard inputs fail explicitly. Keep the installed-wheel proof on shard zero.

Capture subprocess output as bytes in the installed-wheel proof, decoding only
after the process exits. This avoids Windows console encodings crashing Python
reader threads and concealing missing diagnostic output behind a successful
child exit code.
