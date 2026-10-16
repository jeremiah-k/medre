# Firmware-safe Meshtastic pacing and first-bridge guidance

The Meshtastic provider now defaults to 2.2 seconds between outbound messages.
Current firmware rejects phone-originated text less than two seconds apart;
the new default leaves a scheduling margin. Explicit operator overrides remain
available for separately qualified firmware and tests. Local SDK handoff still
does not establish peer reception.

The Getting Started guide now explains a locked Git-based uv deployment,
core/provider ownership, fake smoke evidence, and a real two-way route.
Installation and adapter README navigation point to the same operator flow.
