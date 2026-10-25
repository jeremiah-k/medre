# Native attribution status

Attribution preserves who sent a message and where it came from. The native
sender remains the bridge account or node; readable prefixes and structured
claims are separate properties. Remote metadata does not grant identity or
relation-binding authority.

| Destination adapter | Readable attribution | Structured original-source claims | Evidence and limit |
| --- | --- | --- | --- |
| Matrix | Qualified | Qualified | Frozen 42-direction baseline; native bridge context correlated with canonical ingress. Core #337 and Matrix #21 share the implementation. |
| LXMF | Qualified | Implemented | LXMF #10 passed SDK/codec qualification. Independent native observation of the new complete context remains unqualified; prior native transport used TCP rather than RF. |
| XMPP | Qualified | Candidate qualified on native egress | Prosody TLS delivered full sender/origin claims and an exact source reference to an independent Slixmpp peer. The source was an authored canonical Matrix fixture; this is a destination vertical, not a new all-pairs campaign. |
| Chatmail | Qualified | Pending adapter capability work | Frozen baseline uses Delta Chat core against GreenMail. Ordinary mail headers do not by themselves prove original cross-platform sender identity. |
| SimpleX Chat | Qualified | Pending adapter capability work | Frozen baseline proves readable prefixes, without proving an SDK metadata carrier for original cross-platform attribution. |
| Meshtastic | Qualified | Not carried by the current text adapter | Physical native receipts prove compact readable attribution; no blanket claim is made about every possible protocol payload. |
| MeshCore | Qualified | Not carried by the current text adapter | Physical native receipts distinguish the radio host header from compact original-sender attribution. |

The frozen reference is `medre-native-baseline-2026-10-25/lab-003`: 42 directed
pairs and 168 independent receipts. That reference is regression coverage, not a
reason to repeat the matrix for every new property. New behavior gets targeted
native observations and regression tests in the owning core/adapter PRs.

The XMPP vertical's retained result, source hashes, native XML and server log are
under `/tmp/medre-xmpp-native-2026-10-25/` on the qualification host. Its ordinary
peer receipt is also retained as `tests/fixtures/xmpp-relay-context.xml` for the
independent observation parser. Only the disposable Prosody service was started;
its container, network and volume were removed after evidence capture.
