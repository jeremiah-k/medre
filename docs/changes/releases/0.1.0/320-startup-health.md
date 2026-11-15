# Distinguish started adapters from healthy transports

Probe initial adapter health concurrently within a one-second async budget.
Reflect degraded recovery and unavailable health in the boot summary without
retiring the started instance or its routes. Include initial health in per-instance
startup progress logs.
