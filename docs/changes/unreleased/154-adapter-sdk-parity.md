# Changed

- Audit the LXMF, Meshtastic, and MeshCore adapters against their exact
  pinned SDK versions and add dedicated installed-SDK CI contract tiers.
- Fix real LXMF outbound construction to retain and use the local
  `RNS.Destination` returned by `LXMRouter.register_delivery_identity()` as
  `LXMessage.source`; real-session startup now fails explicitly when the local
  delivery identity cannot be registered.
- Refresh transport setup/limitations documentation for `lxmf==1.1.1`,
  `rns==1.4.2`, `mtjk==2.7.11.post5`, and `meshcore==2.3.8`.
- Pin `rns==1.4.2` explicitly in the LXMF optional extra so installed-SDK
  CI tests the same Reticulum contract recorded by the lockfile.
- Route LXMF inbound stamp cost through `register_delivery_identity()` so it is
  applied to the registered destination, and validate the SDK-supported `0..254`
  range.
- Make MeshCore reconnect ownership explicit with `auto_reconnect=False` and
  remove MEDRE's duplicate `send_appstart()` after SDK factory connection;
  diagnostics now consume the SDK's public `self_info` snapshot.
- Make propagated LXMF delivery operational by configuring an explicit outbound
  propagation-node destination hash and rejecting propagated handoff when no
  node is selected.
- Quiesce each owned `LXMRouter` during stop/reconnect, unregister its
  `atexit` callback, and preserve the process signal handlers
  that existed before router construction without invoking the process-global
  Reticulum exit path.
