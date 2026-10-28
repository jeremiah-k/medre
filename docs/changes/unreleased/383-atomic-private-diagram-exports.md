# Atomic private diagram exports

- Publish locally rendered route diagrams only after the private staging file is
  fully written and synced; never replace an existing destination, including
  an existing symlink or a concurrent writer.
- Sync the published directory entry on POSIX where supported. Unsupported
  directory synchronization leaves a successfully published export intact.
- Correct the Mermaid regression expectation for encoded identifier sigils.
