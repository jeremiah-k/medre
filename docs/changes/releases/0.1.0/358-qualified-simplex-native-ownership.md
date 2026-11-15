# Qualify SimpleX native profile ownership

Pin the merged SimpleX adapter ownership changes in the core compatibility
snapshot. Serialized startup/shutdown records pending worker creation through
repeated cancellation, retains the profile lease during incomplete cleanup and
refuses stale process-group signals. Failed CLI cleanup retains its session
while the Python owner remains alive.

Parent-process-death isolation and independent direct-contact delivery remain
unqualified. Local native queue acceptance does not establish recipient delivery.
The compatibility manifest owns exact source revisions; the evidence index
retains the historical baseline scope.
