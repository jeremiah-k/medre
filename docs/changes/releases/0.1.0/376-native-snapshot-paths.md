# Host-native snapshot filenames and exclusion checks

Allow POSIX-native names such as Matrix account-store databases in selected
stopped-state snapshots. Reject drive spellings on every platform and
colon-bearing paths on Windows before accessing files. Such names require a
POSIX restore host; this does not rename SDK stores or translate identities.
