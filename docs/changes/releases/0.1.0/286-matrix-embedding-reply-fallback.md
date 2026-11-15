# Matrix metadata modes and reply delivery

Matrix rendering honors the destination instance's `off`, `minimal`, `safe`, or
`full` metadata embedding mode. Safe mode exposes normalized metadata and source
message identity; full mode additionally exports canonical native/custom data.
The adapter owns the versioned wire schema. Optional mmrelay fields are controlled
independently by Meshtastic's compatibility flag.

MeshCore and LXMF advertise inline reply fallback through their existing text
renderers, preserving conversation content without emitting native reply
operations. Their delivery evidence records `fallback_text`. Provider fakes now
use the same capabilities for planning and health reporting, including per-instance
MeshCore text budgets.
