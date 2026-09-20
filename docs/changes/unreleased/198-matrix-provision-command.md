# 198: `medre adapter matrix provision` creates private encrypted test spaces

MEDRE could only _consume_ Matrix rooms listed in an adapter allowlist; there
was no supported way to produce the private encrypted room a live smoke
requires. Operators had to create spaces/rooms/invites/power levels by hand.

`medre adapter matrix provision` now provisions one private space and one
private encrypted room (encryption written in the creation `initial_state`,
so the room is encrypted from its first event), links them with
`m.space.child`/`m.space.parent`, invites the requested users to both, and
pre-assigns admin power 100 before the invites take effect — so a join is
immediately an admin, with no watcher loop. The encryption algorithm,
power read-back, and parent/child linkage are verified from actual server
state; invites are reported separately from joins. Requires completed
`adapter matrix auth login` credentials; room/space IDs and permalinks are
printed (IDs are not credentials).

An opt-in live harness (`tests/test_live_matrix_radio_bridge.py`,
`MEDRE_MX_BRIDGE=1`) exercises the six directed Matrix<->radio paths over
one runtime with three explicit bidirectional routes, observes far-side
Megolm decryption via a second bot-account device (own crypto store), asserts
own-account echo suppression, and checks crypto/device continuity across one
controlled restart.
