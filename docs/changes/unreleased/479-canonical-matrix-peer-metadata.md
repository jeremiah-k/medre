# Canonical Matrix peer metadata

Use canonical source identities, scoped native references, structured relations,
and bounded bridge context for shared-room peer metadata. Matrix ingress ignores
flat MMRelay fields for body, sender attribution, and reaction classification;
the Matrix-native schema excludes the relay-specific interop namespace.

Compatibility tests exercise LXMF and XMPP context delivery separately from the
adapter-free core tests. The source-native safe projection and MMRelay consumer
trust migration require their own qualification before the first release tag.
