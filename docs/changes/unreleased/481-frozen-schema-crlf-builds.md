# Digest-proven frozen schema reconstruction

Build exact frozen schema bytes after a Git checkout retains CRLF versions of
unchanged files. Accept LF reconstruction only when it matches the recorded
digest; reject every other edit and package only digest-verified bytes.
