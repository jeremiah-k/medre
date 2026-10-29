# Message presentation and Matrix sending identities

A normal MEDRE bridge sends through its configured account. It can show the
original sender and network in the message, preserve native replies where
supported, and use formatted text where the destination supports it. Changing
presentation does not change which account sent the message.

## Choose the level you need

| Goal                                                  | Support                                                                                                                    |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Show a sender and source network in a Matrix room     | Use source labels and the destination's `relay_prefix`.                                                                    |
| Preserve formatting, native replies or threads        | Use the provider's supported renderer and capability profile; unsupported features fall back as documented.                |
| Change native message layout beyond provider settings | A developer can customize the provider's renderer factory.                                                                 |
| Use a different Matrix client library                 | Requires a compatible provider/session implementation; installing another library alone does not switch MEDRE's transport. |
| Send as separate Matrix virtual users                 | Matrix application-service integration and principal-aware delivery need implementation and qualification.                 |
| Send as a user's existing Matrix account              | Requires explicit account authorization and additional ownership/crypto handling; it is not a display-name setting.        |

The last two are forms of **puppeting**. They are not supported configuration
modes in the distributed Matrix provider. Ordinary bot bridging does not require
you to administer a homeserver.

## Adjust labels first

Set `origin_label` on the **source** adapter. Set `relay_prefix` on the
**destination** adapter. For example, a source label `Forx mesh` and a prefix
`[{origin_label}] {sender}:` followed by a space can produce:

```text
[Forx mesh] Alice: The repeater is back online.
```

The Matrix sending account remains the bridge account. A source label describes
where the message came from; a display prefix is not proof that a Matrix user
named Alice authenticated the send.

The [offline presentation example](../../examples/configs/presentation-fake.yaml)
contains two fake adapters. Install Matrix and Meshtastic, then inspect it without
connecting accounts or radios:

```bash
medre config check --config examples/configs/presentation-fake.yaml
medre smoke --config examples/configs/presentation-fake.yaml --source-adapter radio
```

The example uses in-memory storage and demonstrates local routing/presentation;
it does not establish native recipient delivery. Change only the presentation
settings in your private real configuration after reviewing the example. See
[configuration](configuration.md) for the supported prefix variables and
[transport profiles](../spec/transport-profiles/README.md) for native features.

## Custom rendering and enrichment

Rendering turns a canonical event into the destination's native message shape.
The Matrix provider already emits plain text and formatted HTML where supported;
a different layout is a renderer concern. Retain a readable plain-text body and
escape untrusted display values. Keep source attribution, relation fallback and
size limits intact.

Enrichment adds permitted context before rendering: a resolved reply target,
source label, or retained quote excerpt. It does not grant a new authenticated
identity. Expired or unavailable quote content must degrade honestly; it must
not be fetched from private stores without the applicable access/retention policy.

Custom renderers are packaged through the existing
[provider registration interface](../dev/adapter-registration.md). There is no
operator setting that loads an arbitrary renderer or SDK module by name. The
plugin package is protocol scaffolding, not a deployed event-enrichment loader.
See [messenger interoperability](../dev/messenger-interoperability.md) for the
extension contracts.

## Homeserver administrators and application services

Matrix application services can register user namespaces and assert identities
within their allowed namespace. They require registration with the homeserver;
a client-side prefix or display-name change cannot provide that authority.
See the [Matrix Application Service specification](https://spec.matrix.org/latest/application-service-api/).

[mautrix-python](https://docs.mau.fi/python/latest/) provides Matrix client and
application-service interfaces that could support a separate implementation.
It is a library candidate, not a MEDRE backend that can be enabled today. A
provider implementation must own native transactions, account/device state,
crypto, retries and teardown; a renderer should not create accounts or make
native network calls.

Before offering puppeting, persist a non-secret **sending principal** selection
before native dispatch and keep it fixed through retry and restart. Scoped
native mappings need to identify the principal that created each target copy.
Mutations require authorization for that principal, not just matching display
text. Keep credentials outside event payloads and treat encrypted rooms as a
separately qualified feature of that implementation. None of these changes
restore end-to-end encryption across other networks.
