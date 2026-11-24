"""The published rehearsal must fail on audit or receipt-verification errors."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_DOC = Path(__file__).resolve().parents[1] / "docs/ops/content-ceiling-audit.md"


def _bash_works() -> bool:
    """Whether ``bash`` on the path can run a script.

    Some Windows images ship a stub ``bash.exe`` that only prints an
    install hint for the Linux subsystem and exits nonzero.
    """
    try:
        probe = subprocess.run(
            ["bash", "-c", "true"], capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _bash_works(),
    reason="the rehearsal is a bash script and this platform has no working bash",
)


def _script() -> str:
    text = _DOC.read_text()
    full = text.split("```bash\nset -euo pipefail", 1)[1].split("\n```", 1)[0]
    return "set -euo pipefail\n" + full[full.index("# 3. Enforce in bounded passes") :]


_CLEAN_AUDIT = (
    '{"elapsed_pending":[],"residual_enforced":[],"elapsed_pending_has_more":false,'
    '"residual_enforced_has_more":false}'
)
_PASS_VERIFY = '{"verdict":"passed","receipts_next_after_event":null}'


@pytest.mark.parametrize(
    (
        "audit_exit",
        "verify_exit",
        "audit_json",
        "verify_json",
        "expected_calls",
        "expected_code",
    ),
    [
        (1, 0, _CLEAN_AUDIT, _PASS_VERIFY, ["audit"], 1),
        (0, 0, "{}", _PASS_VERIFY, ["audit"], 2),
        (0, 1, _CLEAN_AUDIT, _PASS_VERIFY, ["audit", "verify"], 1),
        (0, 0, _CLEAN_AUDIT, '{"verdict":"failed"}', ["audit", "verify"], 1),
        (0, 0, _CLEAN_AUDIT, _PASS_VERIFY, ["audit", "verify"], 0),
    ],
)
def test_rehearsal_obeys_failure_boundaries(
    tmp_path: Path,
    audit_exit: int,
    verify_exit: int,
    audit_json: str,
    verify_json: str,
    expected_calls: list[str],
    expected_code: int,
) -> None:
    bash_stub = """
medre() {
    printf '%s\n' "$3" >> "$TRACE"
    if [ "$3" = audit ]; then
        printf '%s\n' "$AUDIT_JSON"
        return "$AUDIT_EXIT"
    fi
    printf '%s\n' "$VERIFY_JSON"
    return "$VERIFY_EXIT"
}
"""
    trace = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "REH": str(tmp_path),
        "TRACE": str(trace),
        "AUDIT_EXIT": str(audit_exit),
        "VERIFY_EXIT": str(verify_exit),
        "AUDIT_JSON": audit_json,
        "VERIFY_JSON": verify_json,
    }
    result = subprocess.run(
        ["bash", "-s"],
        input=bash_stub + _script(),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=15,
    )
    assert result.returncode == expected_code, result.stderr
    assert trace.read_text().splitlines() == expected_calls


def test_rehearsal_shell_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n"], input=_script(), text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
