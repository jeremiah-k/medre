# Preserve complete smoke and drill evidence

Require delivery evidence for all configured route targets matched by a smoke
injection, including targets lost during construction or startup. Select only
started fake sources with an event factory, and stop the runtime when that
factory fails. Describe delivery limitations without assuming a radio platform.

Select adapter-failure drill targets using the actual event's routing filters,
rather than only its source adapter identifier.
