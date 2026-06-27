"""API/export boundary and documentation regression tests.

Verify canonical contract exports, config error hierarchy, credential
sidecar behavior, config error import paths, and absence of stale
references in documentation (sections M–S).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ===================================================================
# M) Canonical core contract exports
# ===================================================================


class TestCanonicalContractExports:
    """Verify that medre.core.contracts.adapter exports the expected
    canonical names.
    """

    _EXPECTED_NAMES = [
        "AdapterContract",
        "AdapterRole",
        "AdapterCodec",
        "AdapterContext",
        "AdapterCapabilities",
        "AdapterInfo",
        "AdapterDeliveryResult",
        "AdapterSendError",
        "AdapterPermanentError",
    ]

    def test_adapter_module_exports(self) -> None:
        import medre.core.contracts.adapter as mod

        for name in self._EXPECTED_NAMES:
            assert hasattr(
                mod, name
            ), f"medre.core.contracts.adapter missing export: {name}"

    def test_contracts_init_reexports(self) -> None:
        import medre.core.contracts as pkg

        for name in self._EXPECTED_NAMES:
            assert hasattr(pkg, name), f"medre.core.contracts missing re-export: {name}"


# ===================================================================
# N) Canonical config error hierarchy
# ===================================================================


class TestConfigErrorHierarchy:
    """Config errors must be ValueError subclasses, not adapter runtime
    errors.
    """

    def test_matrix_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            MatrixConfigError,
        )

        assert issubclass(MatrixConfigError, AdapterConfigError)
        assert issubclass(MatrixConfigError, ValueError)

    def test_meshtastic_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            MeshtasticConfigError,
        )

        assert issubclass(MeshtasticConfigError, AdapterConfigError)
        assert issubclass(MeshtasticConfigError, ValueError)

    def test_meshcore_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            MeshCoreConfigError,
        )

        assert issubclass(MeshCoreConfigError, AdapterConfigError)
        assert issubclass(MeshCoreConfigError, ValueError)

    def test_lxmf_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            LxmfConfigError,
        )

        assert issubclass(LxmfConfigError, AdapterConfigError)
        assert issubclass(LxmfConfigError, ValueError)


# ===================================================================
# O) Config errors are not adapter runtime errors
# ===================================================================


class TestConfigErrorsNotRuntimeErrors:
    """Config errors must not be subclasses of adapter runtime errors."""

    def test_matrix_config_error_not_runtime_error(self) -> None:
        from medre.adapters.matrix.errors import MatrixError
        from medre.config.adapters.errors import MatrixConfigError

        assert not issubclass(MatrixConfigError, MatrixError)

    def test_meshtastic_config_error_not_runtime_error(self) -> None:
        from medre.adapters.meshtastic.errors import MeshtasticError
        from medre.config.adapters.errors import MeshtasticConfigError

        assert not issubclass(MeshtasticConfigError, MeshtasticError)

    def test_meshcore_config_error_not_runtime_error(self) -> None:
        from medre.adapters.meshcore.errors import MeshCoreError
        from medre.config.adapters.errors import MeshCoreConfigError

        assert not issubclass(MeshCoreConfigError, MeshCoreError)

    def test_lxmf_config_error_not_runtime_error(self) -> None:
        from medre.adapters.lxmf.errors import LxmfError
        from medre.config.adapters.errors import LxmfConfigError

        assert not issubclass(LxmfConfigError, LxmfError)


# ===================================================================
# P) Matrix credential sidecar behavior
# ===================================================================


class TestMatrixCredentialSidecar:
    """Verify that Matrix credential sidecar helpers live in the config
    layer and preserve expected behavior.
    """

    def test_get_credentials_path_respects_xdg(self) -> None:
        import os
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import get_credentials_path

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/config"}):
            path = get_credentials_path()
            assert str(path).startswith("/custom/config/")

    def test_get_credentials_path_default(self) -> None:
        import os
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import get_credentials_path

        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            path = get_credentials_path()
            assert ".config" in str(path)
            assert "medre/credentials/matrix.json" in str(path)

    def test_load_credentials_missing_file(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import load_credentials_json

        with patch(
            "medre.config.adapters.matrix_credentials.get_credentials_path"
        ) as mock_path:
            mock_path.return_value = Path("/nonexistent/medre/credentials/matrix.json")
            result = load_credentials_json()
            assert result is None

    def test_load_credentials_valid_json(self, tmp_path: Any) -> None:
        import json
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import load_credentials_json

        cred_file = tmp_path / "matrix.json"
        cred_file.write_text(
            json.dumps(
                {
                    "homeserver": "https://matrix.org",
                    "user_id": "@bot:matrix.org",
                    "access_token": "syt_abc123",
                }
            )
        )

        with patch(
            "medre.config.adapters.matrix_credentials.get_credentials_path",
            return_value=cred_file,
        ):
            result = load_credentials_json()
            assert result is not None
            assert result["homeserver"] == "https://matrix.org"
            assert result["user_id"] == "@bot:matrix.org"

    def test_load_credentials_invalid_json(self, tmp_path: Any) -> None:
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import load_credentials_json

        cred_file = tmp_path / "matrix.json"
        cred_file.write_text("not valid json{{{")

        with patch(
            "medre.config.adapters.matrix_credentials.get_credentials_path",
            return_value=cred_file,
        ):
            result = load_credentials_json()
            assert result is None


# ===================================================================
# Q) ConfigError import from canonical locations
# ===================================================================


class TestConfigErrorCanonicalImports:
    """ConfigError classes must be imported from medre.config.adapters.errors."""

    # These are allowed paths for importing ConfigError classes.
    _ALLOWED_ERROR_PATHS = ("from medre.config.adapters.errors import ",)

    # These are FORBIDDEN — ConfigError classes must not be imported
    # from config dataclass modules.
    _FORBIDDEN_ERROR_IMPORTS = (
        "from medre.config.adapters.matrix import MatrixConfigError",
        "from medre.config.adapters.meshtastic import MeshtasticConfigError",
        "from medre.config.adapters.meshcore import MeshCoreConfigError",
        "from medre.config.adapters.lxmf import LxmfConfigError",
    )

    def test_config_errors_not_imported_from_dataclass_modules(self) -> None:
        """No source or test file should import ConfigError from dataclass modules."""
        repo_root = Path(__file__).resolve().parents[1]
        violations: list[str] = []
        for py_file in sorted((repo_root / "src").rglob("*.py")):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(p in stripped for p in self._FORBIDDEN_ERROR_IMPORTS):
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )
        for py_file in sorted((repo_root / "tests").rglob("*.py")):
            # Exclude test files that define the forbidden patterns as literals.
            if py_file.name in ("test_boundary_api.py",):
                continue
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(p in stripped for p in self._FORBIDDEN_ERROR_IMPORTS):
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )

        assert violations == [], (
            "ConfigError imports from dataclass modules found (must use medre.config.adapters.errors):\n"
            + "\n".join(violations)
        )

    def test_config_adapters_no_facade_re_exports(self) -> None:
        """config/adapters/__init__.py must not re-export error types."""
        import medre.config.adapters as mod

        for name in (
            "MatrixConfigError",
            "MeshtasticConfigError",
            "MeshCoreConfigError",
            "LxmfConfigError",
        ):
            assert not hasattr(mod, name), (
                f"medre.config.adapters should not re-export {name}; "
                f"import from medre.config.adapters.errors instead"
            )


# ===================================================================
# R) No active stale architecture references in docs
# ===================================================================


class TestNoActiveStaleDocsReferences:
    """No active documentation should reference removed modules as if current.

    docs/ARCHITECTURE_PLAN.md may mention removed modules only in
    "does not exist" / "must not be imported" factual context.
    """

    _STALE_PATTERNS = (
        "BaseAdapter",
        "medre.adapters.base",
        "adapters/base.py",
        "medre.core.ports",
        "core/ports.py",
        "medre.core.adapter_base",
        "core/adapter_base.py",
        "medre.adapters.matrix.config",
        "medre.adapters.meshtastic.config",
        "medre.adapters.meshcore.config",
        "medre.adapters.lxmf.config",
        "adapters/matrix/config.py",
        "adapters/meshtastic/config.py",
        "adapters/meshcore/config.py",
        "adapters/lxmf/config.py",
        "medre.adapters.matrix.errors.MatrixConfigError",
        "medre.adapters.meshtastic.errors.MeshtasticConfigError",
        "medre.adapters.meshcore.errors.MeshCoreConfigError",
        "medre.adapters.lxmf.errors.LxmfConfigError",
    )

    _ALLOWED_CONTEXT_WORDS = (
        "does not exist",
        "must not be imported",
        "noncanonical",
    )

    def test_no_active_stale_references_in_docs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        assert docs_dir.exists()

        violations: list[tuple[str, int, str]] = []

        for md_file in sorted(docs_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")

            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not any(pattern in stripped for pattern in self._STALE_PATTERNS):
                    continue

                # Allow lines with factual-context markers (e.g. "removed",
                # "merged", "replaced", "historical", etc.)
                lowered = stripped.lower()
                if any(word in lowered for word in self._ALLOWED_CONTEXT_WORDS):
                    continue

                violations.append((str(md_file.relative_to(repo_root)), i, stripped))

        assert (
            not violations
        ), "Active stale architecture references found in docs:\n" + "\n".join(
            f"{f}:{line}: {s}" for f, line, s in violations
        )


# ===================================================================
# S) No stale transitional wording in documentation
# ===================================================================


class TestNoStaleWordingInDocs:
    """Scan docs/**/*.md for discouraged transitional/historical phrases.

    This test catches phrasing that frames the current architecture as
    transitional, legacy, or historical — rather than as the intended
    architecture from the start.

    Allowed exceptions:
    - Lines containing precise removal statements (e.g. "was replaced by",
      "was removed", "does not exist").
    """

    _FORBIDDEN_PHRASES = (
        "legacy adapter framework",
        "legacy adapter layer",
        "historical architecture",
        "compatibility shim",
        "compatibility layer",
        "pre-refactor architecture",
        "transitional import path",
        "migration-era",
        "old adapter framework",
        "old architecture",
    )

    # Lines containing these phrases are exempt — they are factual
    # noncanonical-module statements, not transitional framing.
    _EXEMPTION_WORDS = (
        "does not exist",
        "must not be imported",
        "noncanonical",
        # Negation context — "no compatibility shims", "not a compatibility layer"
        "no ",
        "no.",
        "not ",
        "not.",
        "without",
        # Module description context — compat.py file tree comments
        "compat.py",
    )

    # These files are fully exempt from the wording check.
    _EXEMPT_FILES: frozenset[str] = frozenset()

    def test_no_stale_wording_in_docs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        assert docs_dir.exists()

        violations: list[tuple[str, int, str]] = []

        for md_file in sorted(docs_dir.rglob("*.md")):
            if md_file.name in self._EXEMPT_FILES:
                continue

            text = md_file.read_text(encoding="utf-8")

            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                lowered = stripped.lower()

                if not any(phrase in lowered for phrase in self._FORBIDDEN_PHRASES):
                    continue

                # Exempt lines that are factual removal/merge statements
                if any(word in lowered for word in self._EXEMPTION_WORDS):
                    continue

                violations.append((str(md_file.relative_to(repo_root)), i, stripped))

        assert (
            not violations
        ), "Stale transitional wording found in docs:\n" + "\n".join(
            f"{f}:{line}: {s}" for f, line, s in violations
        )
