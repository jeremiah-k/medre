# Navigate command help from registered command paths

`medre help [command ...]`, `medre --help routes ...`, and bare command groups
show relevant help without executing a command handler. Compact usage headings
and configuration-discovery hints make nested commands and defaults visible.
Explicit help selects the registered parser before argument parsing, including
commands whose remaining arguments would otherwise be delegated to a subprocess.
