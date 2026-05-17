# CLI Command-Contribution Registry

## Overview

The MEDRE CLI separates **core product commands** from **adapter/plugin contributed
commands**.  Core commands are defined directly in `main.py`.  Contributed commands
live under reserved top-level namespaces and are registered through the
contribution registry in `contrib.py`.

This separation ensures that `medre --help` never imports optional SDK packages
(nio, meshtastic, RNS, LXMF).  Parser construction and help formatting touch only
`argparse` — no transport code is loaded until the user explicitly invokes a
subcommand that needs it.

## Core product commands (flat)

These are defined directly in `_build_parser()` inside `main.py`:

| Command | Description |
|---------|-------------|
| `config check` | Validate config file |
| `config sample` | Print sample config |
| `routes validate` | Validate route configuration |
| `routes topology` | Print route topology preview |
| `routes list` | List configured routes |
| `run` | Start the runtime |
| `diagnostics` | Pre-flight runtime snapshot |
| `inspect` | Read-only event/receipt investigation |
| `replay` | Re-execute stored events |
| `smoke` | Local validation tooling |
| `trace` | Timeline assembly |
| `evidence` | Support bundle collection |
| `recover` | Recovery classification |
| `paths` | Print resolved paths |
| `version` | Print version |
| `adapters` | List available adapters |

## Contributed command namespaces

Adapter and plugin commands live under reserved namespaces:

| Namespace | Purpose | Current subcommands |
|-----------|---------|-------------------|
| `auth` | Authentication flows | `auth matrix login` |
| `adapter` | Adapter-specific operations | *(not yet populated)* |
| `plugin` | Plugin extensions | *(not yet populated)* |

## Registration

Contributed commands are registered through
`contrib.register_builtin_contributors(subparsers)`, called at the end of
`_build_parser()`.  Each namespace has a private `_register_*` helper in
`contrib.py` that adds the argparse subparsers and arguments.

No external plugin discovery mechanism exists yet.  All contributors are
built-in and registered in deterministic order.

## Dispatch

`main()` delegates contributed commands to
`contrib.dispatch_contribution(args)`, which matches on the parsed namespace
and lazy-imports only the command module needed for that specific invocation.

## Lazy-load invariant

`medre --help` must not import any optional SDK.  This is enforced by:

1. `contrib.py` imports nothing SDK-related at module level.
2. `dispatch_contribution()` uses `from .auth_commands import _auth_matrix_login`
   inside the matched branch — not at module level.
3. `_build_parser()` only calls argparse registration helpers that define
   argument schemas, never import transport code.

## Namespace rules

```python
ALLOWED_NAMESPACES = ("auth", "adapter", "plugin")
DISALLOWED_TOPLEVEL = ("matrix", "meshtastic", "lxmf", "meshcore")
```

- Transport names (matrix, meshtastic, lxmf, meshcore) must never appear as a
  top-level command.  They belong under a namespace (`auth matrix`, `adapter
  matrix`, etc.).
- Only `auth`, `adapter`, and `plugin` are valid top-level namespaces for
  contributed commands.

## Current state

Only `auth matrix login` exists today.  The `adapter` and `plugin` namespaces
are reserved but not yet populated.  When new adapter-specific commands are
needed, they should be added as `_register_*` helpers in `contrib.py` and called
from `register_builtin_contributors()`.
