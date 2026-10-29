# medre-test-tier: compatibility
"""Prevent LXMF setup examples from regressing behind native worker ownership."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_DOC = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "ops"
    / "transport-setup"
    / "lxmf.md"
)


def test_real_lxmf_config_examples_include_required_private_storage() -> None:
    """Every documented real LXMF constructor must pass the required store."""
    source = _DOC.read_text(encoding="utf-8")
    snippets = re.findall(r"```python\n(.*?)\n```", source, flags=re.DOTALL)
    assert snippets, "Expected documented Python configuration examples"
    real_examples = 0
    for snippet in snippets:
        tree = ast.parse(snippet)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "LxmfConfig":
                continue
            options = {item.arg: item.value for item in node.keywords}
            mode = options.get("connection_type")
            if not isinstance(mode, ast.Constant) or mode.value != "reticulum":
                continue
            real_examples += 1
            path = options.get("storage_path")
            assert isinstance(path, ast.Constant) and isinstance(path.value, str)
            assert path.value.strip(), "Native LXMF needs an explicit storage_path"
    assert real_examples >= 2, "Both real-mode examples must stay executable"


def test_lxmf_operator_guide_preserves_identity_and_shared_service_boundary() -> None:
    """Operator instructions must not invent disposable identities or daemon conflict."""
    source = _DOC.read_text(encoding="utf-8")
    assert "{storage_path}/.medre-identity" in source
    assert "same identity on restart" in source
    assert "handoff journal" in source
    assert "share a running `rnsd`" in source
    assert "native worker" in source
    assert "not persisted across restarts" not in source
    assert "rnsd conflicts" not in source
