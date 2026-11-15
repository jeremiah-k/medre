# Explicit package source initialization

`medre init` creates the adapter source catalog in MEDRE's configuration
folder, or at `--sources PATH`, without installing packages. Repeating the
command preserves and validates the existing catalog. Core source selection
and updates retain uv's installation receipt as their authority.
