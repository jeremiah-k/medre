# Adopt provider stopped-state declarations and native capture

Pin all seven adapters to merged revisions exposing their provider-owned
stopped-state declarations. Include the Chatmail single-account native resolver
and the LXMF endpoint capture, exit-persistence ownership, and bounded
pre-handoff destination discovery.

Align the provider inventory guide with retained-exclusion capture. Shared
Reticulum services can remain active while MEDRE's separate LXMF endpoint is
captured; their network state requires a separate backup. Chatmail and LXMF
reject hard-linked native files. Restored accepted and uncertain handoffs retain
their existing submission fences.
