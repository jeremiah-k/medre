# XMPP transport setup

The XMPP provider bridges live text between configured multi-user chat rooms
(MUC), direct peers, and other MEDRE adapters. It uses Slixmpp for TLS,
authentication, room membership, and protocol negotiation.

## Install and prepare an account

```bash
medre adapters install xmpp
medre adapters
```

For a locked deployment, add the provider to the same project as MEDRE and use
`uv run --locked medre` for the commands below. See [installation](../install.md).
Use a dedicated XMPP account, a server with a trusted TLS certificate, and
permission to join each configured room. Account creation is separate from MEDRE.

## Configure native conversations

Save a private `connections.yaml` with the connection and empty routes:

```yaml
adapters:
  xmpp:
    chat:
      adapter_kind: real
      jid: bridge@example.org/medre
      nickname: medre
      rooms: [bridge@conference.example.org]
      peers: [friend@example.org]
      origin_label: XMPP
routes: {}
```

Supply the password as `MEDRE_ADAPTER__CHAT__PASSWORD` through the service
manager or secret mechanism used by the deployment. Keep credentials out of
shared YAML. Room and peer lists use exact, lowercase bare JIDs; do not include
occupant nicknames or a contact resource suffix. A conversation cannot appear
in both lists. Optional `host` and `port` overrides are supplied together;
omit both for DNS SRV discovery. TLS certificate verification stays enabled.
TLS transport encryption is separate from end-to-end message encryption.

Add another adapter instance, then build and inspect the bridge:

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

Use `bridge@conference.example.org` or `friend@example.org` as the context,
matching the allowlist. Startup joins every configured room before readiness.
The provider reconnects and rejoins after a transport outage; authentication
rejection needs operator action.

## Behavior and evidence

Text, destination-scoped native replies with readable fallback, source
attribution, and opt-in negotiated typing are supported. Delayed history,
self echoes, unsupported mutation bodies, and bodyless controls are excluded.
This live bridge does not retrieve offline messages or room archives.
Attachments, OMEMO, read receipts, and puppeting are unsupported.

A successful send confirms a local TLS transport write. A correlated own-room
echo can supply a native reply reference; it does not prove recipient delivery.
See the [profile](../../spec/transport-profiles/xmpp.md) and provider-owned
[configuration schema](https://github.com/jeremiah-k/medre-adapter-xmpp/blob/main/docs/schemas/config.schema.json)
for the full contract. Test both bridge directions from independent accounts.
