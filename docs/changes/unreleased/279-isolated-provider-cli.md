# Isolated adapter CLI contributions

Core now owns each `medre adapter <transport>` parser. Provider registration
hooks receive that parser directly and configure their own arguments and
subcommands. The previous shared subparser-collection callback is removed
before the first release; Matrix's contribution uses the new contract.

Missing or invalid CLI symbols, registration exceptions, and argument/default
collisions with core dispatch fields are reported as broken CLI contributions
by adapter preflight. They do not disable basic help, version, healthy commands,
or the provider's runtime registration. Partial command trees are discarded and
displayed exception details are sanitized.
