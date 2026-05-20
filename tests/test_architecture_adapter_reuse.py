"""Architecture boundary tests: reusable adapter module boundaries.

Ensures codec/renderer/interop/session modules don't import runtime, CLI,
storage, engine, adapter wrappers, or protocol SDKs at module level.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.ast_imports import (
    import_matches,
    parse_python,
    runtime_scope_imports,
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"

# Reusable modules that should be importable without side effects
_REUSABLE_MODULES: list[tuple[str, str]] = [
    ("src/medre/adapters/matrix/codec.py", "matrix"),
    ("src/medre/adapters/matrix/renderer.py", "matrix"),
    ("src/medre/adapters/meshtastic/codec.py", "meshtastic"),
    ("src/medre/adapters/meshtastic/renderer.py", "meshtastic"),
    ("src/medre/adapters/meshcore/codec.py", "meshcore"),
    ("src/medre/adapters/meshcore/renderer.py", "meshcore"),
    ("src/medre/adapters/lxmf/codec.py", "lxmf"),
    ("src/medre/adapters/lxmf/renderer.py", "lxmf"),
    ("src/medre/adapters/matrix/session.py", "matrix"),
    ("src/medre/adapters/meshtastic/session.py", "meshtastic"),
    ("src/medre/adapters/meshcore/session.py", "meshcore"),
    ("src/medre/adapters/lxmf/session.py", "lxmf"),
    ("src/medre/interop/mmrelay.py", ""),
]

# Forbidden for codec/renderer modules
_CODEC_RENDERER_FORBIDDEN: tuple[str, ...] = (
    "medre.runtime",
    "medre.cli",
    "medre.core.engine",
    "medre.core.storage",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
)

# SDKs that codec/renderer must not import
_HEAVY_SDKS: tuple[str, ...] = ("nio", "meshtastic", "meshcore", "RNS", "lxmf")

# Session modules may import their own SDK but not others
_SESSION_FORBIDDEN: tuple[str, ...] = (
    "medre.runtime",
    "medre.cli",
    "medre.core.engine",
    "medre.core.storage",
)

# Per-transport SDK allowlists for session modules
_TRANSPORT_SDKS: dict[str, tuple[str, ...]] = {
    "matrix": ("nio",),
    "meshtastic": ("meshtastic", "serial", "serial_asyncio"),
    "meshcore": ("meshcore",),
    "lxmf": ("RNS", "lxmf"),
}


def _check_module(
    py_file: Path,
    transport: str,
    is_session: bool = False,
) -> list[str]:
    """Check a reusable module for forbidden imports."""
    violations: list[str] = []
    tree = parse_python(py_file)
    imports = runtime_scope_imports(tree, file_path=str(py_file))
    rel = str(py_file.relative_to(_REPO))

    # Resolve transport-specific SDK allowlist for session modules
    allowed_sdks: tuple[str, ...] = ()
    if is_session and transport in _TRANSPORT_SDKS:
        allowed_sdks = _TRANSPORT_SDKS[transport]

    for imp in imports:
        mod = imp.module

        # Check banned prefixes for all reusable modules
        banned = _CODEC_RENDERER_FORBIDDEN if not is_session else _SESSION_FORBIDDEN
        if import_matches(mod, banned):
            violations.append(f"{rel}:{imp.lineno}: banned import {mod}")
            continue

        # Check own-adapter import (codec should not import its own adapter)
        if transport and import_matches(mod, (f"medre.adapters.{transport}.adapter",)):
            violations.append(
                f"{rel}:{imp.lineno}: imports own adapter wrapper: {mod}"
            )
            continue

        # Check cross-adapter imports
        other_transports = [t for t in ["matrix", "meshtastic", "meshcore", "lxmf"]
                          if t != transport]
        for ot in other_transports:
            if import_matches(mod, (f"medre.adapters.{ot}",)):
                violations.append(
                    f"{rel}:{imp.lineno}: imports cross-adapter module {mod}"
                )
                break

        # Check heavy SDKs (for codec/renderer only; session modules allow own SDK)
        if not is_session:
            for sdk in _HEAVY_SDKS:
                top_level = mod.split(".")[0]
                if top_level == sdk or mod.startswith(sdk + "."):
                    violations.append(
                        f"{rel}:{imp.lineno}: imports SDK {sdk} at module level"
                    )
                    break
        elif allowed_sdks:
            # Session modules: allow their own transport's SDK, forbid others
            top_level = mod.split(".")[0]
            is_allowed = any(
                top_level == s or mod.startswith(s + ".") for s in allowed_sdks
            )
            if not is_allowed:
                for sdk in _HEAVY_SDKS:
                    if top_level == sdk or mod.startswith(sdk + "."):
                        violations.append(
                            f"{rel}:{imp.lineno}: session imports "
                            f"foreign SDK {sdk} at module level"
                        )
                        break

    return violations


class TestCodecRendererBoundary:
    """Codec, renderer, interop, and session modules must not import runtime, SDKs, or adapters."""

    @pytest.mark.parametrize("rel_path,transport", _REUSABLE_MODULES)
    def test_module_no_forbidden_imports(self, rel_path: str, transport: str) -> None:
        py_file = _REPO / rel_path
        assert py_file.exists(), f"File not found: {py_file}"
        is_session = "session" in rel_path
        violations = _check_module(py_file, transport, is_session=is_session)
        assert not violations, (
            f"{rel_path} has forbidden imports:\n" + "\n".join(violations)
        )
