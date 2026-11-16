"""Platform-native display of process argument vectors, without runtime imports."""

from __future__ import annotations

import os
import re
import shlex


def _command_example(*args: str) -> str:
    """Render a copyable platform-native command even for paths with spaces."""
    if os.name == "nt":
        # PowerShell single quotes preserve &, $, backticks and embedded quotes.
        def literal(arg: str) -> str:
            if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", arg):
                return arg
            return "'" + arg.replace("'", "''") + "'"

        command = " ".join(literal(arg) for arg in args)
        return "& " + command if args and literal(args[0]).startswith("'") else command
    return shlex.join(args)
