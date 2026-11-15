# Preserve Matrix device and store ownership in native qualification

- Give Docker Matrix tests independent device logins and persistent stores;
  retain both across restart checks instead of replacing a device's crypto keys.
- Wire encrypted-room tests through durable admission, checkpoints, and core's
  ingress worker. Keep ordinary signing-bootstrap stores separate from stores
  adopted by durable sync.
- Observe decrypted media through SDK sync responses and typed encrypted-media
  events, matching the exact delivered event before comparing attachment bytes.
- Align the non-durable client configuration expectation with mindroom-nio 1.1.5.
