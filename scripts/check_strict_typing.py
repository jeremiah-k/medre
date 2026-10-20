"""Run reproducible incremental strict-mypy checks across MEDRE distributions.

Usage: python scripts/check_strict_typing.py [--workspace PATH] [--all]

The default is a passing gate for the contracts and transport packages already
migrated. --all audits every package and fails until the remaining migrations
are complete. Optional transport SDK imports are exempt per module, so any
other unresolved import — including a misspelled MEDRE-owned module — fails
the gate instead of degrading to Any.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_READY: dict[str, tuple[str, ...]] = {
    "medre": (
        "src/medre/core/contracts",
        "src/medre/adapter_spec.py",
        "src/medre/adapter_registry.py",
    ),
    "medre-adapter-lxmf": ("src/medre",),
    "medre-adapter-meshcore": ("src/medre",),
    "medre-adapter-meshtastic": ("src/medre",),
}
_REMAINING = (
    "medre-adapter-matrix",
    "medre-adapter-xmpp",
    "medre-adapter-chatmail",
    "medre-adapter-simplexchat",
)

# Optional transport SDK modules per repository: absent from the offline
# typing toolchain, so their imports resolve as Any instead of failing the
# gate. Every repository the gate checks declares its set here, and any
# undeclared unresolved module is a hard error.
_OPTIONAL_SDK_MODULES: dict[str, tuple[str, ...]] = {
    "medre": (),
    "medre-adapter-lxmf": ("LXMF", "RNS"),
    "medre-adapter-meshcore": ("bleak", "meshcore"),
    "medre-adapter-meshtastic": ("meshtastic", "pubsub"),
    "medre-adapter-matrix": ("aiohttp", "nio"),
    "medre-adapter-xmpp": ("slixmpp",),
    "medre-adapter-chatmail": ("deltachat_rpc_client",),
    "medre-adapter-simplexchat": ("simplex_chat",),
}


def _sdk_exemption_config(modules: tuple[str, ...]) -> str:
    lines = ["[mypy]"]
    for module in modules:
        lines.append(f"[mypy-{module},{module}.*]")
        lines.append("ignore_missing_imports = True")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--all", action="store_true", help="audit all source files and adapters"
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    core = workspace / "medre"
    targets = (
        {name: ("src/medre",) for name in (*_READY, *_REMAINING)}
        if args.all
        else _READY
    )
    result = 0
    for name, paths in targets.items():
        repo = workspace / name
        if not repo.is_dir():
            print(f"{name}: repository missing at {repo}", file=sys.stderr)
            result = 1
            continue
        src_paths = [str(repo / "src")]
        if repo != core:
            src_paths.append(str(core / "src"))
        env = os.environ.copy()
        # Workspace roots come first so caller-provided stub paths cannot
        # shadow the trees under check; caller entries remain searchable.
        caller_paths = env.get("MYPYPATH", "")
        if caller_paths:
            src_paths.append(caller_paths)
        env["MYPYPATH"] = os.pathsep.join(src_paths)
        print(f"\n== {name} ==", flush=True)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ini", delete=False
        ) as config_file:
            config_file.write(_sdk_exemption_config(_OPTIONAL_SDK_MODULES[name]))
        try:
            check = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--python-version=3.11",
                    "--explicit-package-bases",
                    "--follow-imports=silent",
                    "--config-file",
                    config_file.name,
                    "--no-incremental",
                    *paths,
                ],
                cwd=repo,
                env=env,
                check=False,
            )
        finally:
            os.unlink(config_file.name)
        result |= int(check.returncode != 0)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
