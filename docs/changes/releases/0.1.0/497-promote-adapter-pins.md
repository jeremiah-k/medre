# Promote the adapter pins

Promote the Meshtastic adapter revision that drops the last adapter import
of `medre.interop.mmrelay`: the reaction sentinel now lives in the classifier
as a native decoded-packet property with its wire semantics documented at the
definition, and classification behavior is unchanged. With this revision
pinned, no adapter in the compatibility manifest imports the interop module
and its core retirement is unblocked.

Promote the remaining adapter revisions carrying the terminology corrections:
present-tense contracts replace historical framing in meshcore send-path
docstrings and simplexchat sealed-request entries, and the matrix, meshtastic,
lxmf, xmpp, and chatmail test suites name the text-field selection and
stale-text suppression contracts by their standing behavior. No adapter wire
behavior changes.
