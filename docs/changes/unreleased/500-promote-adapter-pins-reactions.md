# Promote the adapter pins

Promote the Matrix adapter revision that delivers cross-network reactions as
described attributed `m.emote` messages rather than native `m.reaction`
annotations issued under the bridge identity: every delivered reaction now
carries the reacting source user's attribution, the exact reaction key, and a
bounded one-line preview of the target text, degrading honestly when the
target text is unresolvable and respecting configured output budgets in both
delivery strategies. The Matrix transport profile follows the adapter: the
reactions capability reads `"fallback"`, the relation-degradation row names
the described emote rendering path, and the end-to-end-encryption notes
attribute plaintext reaction events to observed traffic only.

Promote the remaining adapter revisions carrying release-workflow updates
with no wire-behavior change: Meshtastic, LXMF, XMPP, Chatmail, and
SimpleX Chat.
