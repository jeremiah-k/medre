"""Documentation security hygiene tests.

Ensures that token-shaped secret-looking values (e.g. ``syt_…``) do not
appear in example configurations, code blocks, or schema examples.
Realistic secret patterns in docs normalise credential leakage.

``syt_`` is permitted **only** in prose that describes the token pattern
for security/redaction guidance (e.g. "search for ``syt_`` to redact tokens").
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _ROOT / "docs"

# Patterns to scan
_SCAN_GLOBS = [
    "docs/**/*.md",
    "docs/**/*.json",
    "docs/**/*.yaml",
    "docs/**/*.yml",
]

# Prose keywords that indicate the line is describing ``syt_`` for
# security/redaction guidance rather than using it as a config value.
# These are applied per-line; a match on any keyword allows the occurrence.
_ALLOW_KEYWORDS = ("search", "redact", "redaction", "preview", "3-character")


def _collect_files() -> list[Path]:
    """Collect all documentation files that should be scanned."""
    files: list[Path] = []
    for glob_pattern in _SCAN_GLOBS:
        files.extend(_ROOT.glob(glob_pattern))
    return sorted(set(files))


def _is_allowed(line: str) -> bool:
    """Return True if this ``syt_`` occurrence is in security/redaction prose."""
    lower = line.lower()
    return any(kw in lower for kw in _ALLOW_KEYWORDS)


# ===========================================================================
# Tests
# ===========================================================================


class TestNoTokenShapedSecretsInExamples:
    """``syt_`` token values must not appear in example configs or code blocks.

    Only security/redaction guidance prose may reference ``syt_`` as a
    pattern to watch for.
    """

    @pytest.mark.parametrize(
        "doc_file",
        _collect_files(),
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_no_syt_tokens_in_examples(self, doc_file: Path) -> None:
        text = doc_file.read_text(encoding="utf-8")
        rel = str(doc_file.relative_to(_ROOT))
        violations: list[str] = []

        for lineno, line in enumerate(text.splitlines(), start=1):
            if "syt_" not in line:
                continue
            if _is_allowed(line):
                continue
            violations.append(f"  {rel}:{lineno}: {line.strip()}")

        assert not violations, (
            f"Found {len(violations)} line(s) with token-like ``syt_`` values "
            f"in example configs/code blocks. "
            f"Replace with ``<matrix-access-token>`` or add to allowlist if this "
            f"is security/redaction guidance prose:\n" + "\n".join(violations)
        )
