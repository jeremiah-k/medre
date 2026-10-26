# 303: Explicit uv-managed adapter source commands

Add explicit `medre adapters sources`, `install`, and `remove` commands with a
configurable YAML source catalog defaulting to the first-party Git repositories.
Preserve unrelated tool requirements and refuse environments or receipt settings
that cannot be managed faithfully. `medre update` delegates updates to uv while
retaining source pins; package changes support command previews with `--dry-run`.
