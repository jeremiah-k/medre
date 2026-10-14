"""Cold import probes shared by core and installed-provider qualification."""

import json
import subprocess
import sys
import textwrap

from medre.runtime.architecture_report import _SDK_PACKAGES as _FORBIDDEN_SDKS

# Modules that should NOT be imported as a side effect
_FORBIDDEN_SIDE_EFFECTS: tuple[str, ...] = (
    "medre.runtime.builder",
    "medre.core.engine.pipeline",
    "medre.core.storage",
    "medre.core.storage.sqlite",
    "medre.adapters.matrix.adapter",
    "medre.adapters.meshtastic.adapter",
    "medre.adapters.meshcore.adapter",
    "medre.adapters.lxmf.adapter",
)


def check_in_subprocess(module_name: str) -> dict:
    """Import *module_name* in a cold subprocess, return side-effect report."""
    code = textwrap.dedent(f"""\
        import json, sys, logging
        forbidden_side = {list(_FORBIDDEN_SIDE_EFFECTS)!r}
        forbidden_sdks = {list(_FORBIDDEN_SDKS)!r}
        forbidden = set(forbidden_side + forbidden_sdks)

        before = set(sys.modules)
        root_level_before = logging.root.level
        root_handlers_before = len(logging.root.handlers)

        import {module_name}  # noqa: F401

        after = set(sys.modules)
        new_modules = after - before
        forbidden_loaded = sorted(m for m in new_modules
                                   if any(m == f or m.startswith(f + ".") for f in forbidden))

        result = {{
            "forbidden_loaded": forbidden_loaded,
            "root_level_changed": logging.root.level != root_level_before,
            "new_handlers": len(logging.root.handlers) - root_handlers_before,
        }}
        print(json.dumps(result))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(proc.stdout.strip())
