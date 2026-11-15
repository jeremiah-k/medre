# Capture and restore selected stopped state

Verify selected core, adapter and native files as one maintenance snapshot.
Require an explicit SQLite or opaque capture format independently of role.
Reject malformed declarations and opaque inputs with uncheckpointed journals.
Plaintext SQLite files retain committed WAL through SQLite backup. Restore
compares copies with the originally verified digests and reserves an absent
destination without replacing a competing directory. Provider inventory and
stopped native ownership remain caller prerequisites; checksums do not authorize
replay or establish authenticity.
