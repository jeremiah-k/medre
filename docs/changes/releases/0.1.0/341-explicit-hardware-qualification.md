# Separate service setup from radio qualification

Docker provisioning no longer opens radio hardware. The explicit hardware stage
pauses MeshCore after its first failed communication attempt and requires an
operator repair acknowledgement before another probe. Independent service and
radio outcomes remain recorded. MeshCore self-info is reused from the SDK's
initial handshake rather than issuing another APP_START.
