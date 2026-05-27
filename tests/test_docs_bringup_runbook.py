"""Bring-up runbook tests (classes 21-27).

Asserts that the cross-transport Matrix ↔ Meshtastic bring-up runbook
(live-validation/matrix-meshtastic.md) follows auth-first workflow, has no
stale PYTHONPATH, documents snapshot path arguments, diagnostics description,
targeting fields, secure credentials, and operator answerability.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = _ROOT / "docs" / "ops"

_BRINGUP = OPS_DIR / "live-validation" / "matrix-meshtastic.md"
_SECURE_CREDS = OPS_DIR / "configuration.md"


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


# ===========================================================================
# 21. Bring-up runbook: auth-first workflow
# ===========================================================================


class TestBringupRunbookAuthFirst:
    """The bring-up runbook must describe auth-first: obtain credentials via
    ``medre adapter matrix auth login`` *before* running ``medre run``."""

    def test_bringup_mentions_auth_login(self) -> None:
        """bringup runbook must contain ``medre adapter matrix auth login``."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        assert "medre adapter matrix auth login" in text, (
            "live-validation/matrix-meshtastic.md must mention "
            "'medre adapter matrix auth login' in the auth-first workflow."
        )

    def test_auth_appears_before_run(self) -> None:
        """The first ``medre adapter matrix auth`` mention must appear before
        the first ``medre run`` mention."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        auth_pos = text.find("medre adapter matrix auth")
        run_pos = text.find("medre run")
        assert (
            auth_pos >= 0
        ), "bringup runbook must mention 'medre adapter matrix auth login'."
        assert run_pos >= 0, "bringup runbook must mention 'medre run'."
        assert auth_pos < run_pos, (
            "bringup runbook must present 'medre adapter matrix auth' before "
            "'medre run' "
            "(auth-first workflow: obtain token before starting runtime)."
        )


# ===========================================================================
# 22. Bring-up runbook: no stale PYTHONPATH=src
# ===========================================================================


class TestBringupRunbookNoStalePythonpath:
    """The bring-up runbook targets live hardware operators. ``PYTHONPATH=src``
    is a developer-only concern and should only appear in source-checkout
    context (near words like ``source`` or ``checkout``), ideally not at all."""

    def test_pythonpath_only_in_source_context(self) -> None:
        """Any ``PYTHONPATH=src`` in bringup must be near source/checkout
        context words. Ideally there should be none."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        violations: list[str] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "PYTHONPATH=src" not in line:
                continue
            # Allow if the line or nearby context mentions source/checkout.
            context_start = max(0, text.find(line) - 200)
            context_end = text.find(line) + len(line) + 200
            context = text[context_start:context_end].lower()
            if "source" in context or "checkout" in context:
                continue
            violations.append(f"line {lineno}: {line.strip()}")
        assert not violations, (
            "live-validation/matrix-meshtastic.md contains PYTHONPATH=src "
            "without source/checkout context. Bring-up operators should "
            "not need developer PYTHONPATH. Violations:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# 23. Bring-up runbook: --snapshot-on-shutdown requires path argument
# ===========================================================================


class TestBringupRunbookSnapshotRequiresPath:
    """Every ``--snapshot-on-shutdown`` example in the bring-up runbook must
    be followed by a non-flag path token (e.g. a file path), not left bare."""

    def test_snapshot_flag_has_path_argument(self) -> None:
        """Every ``--snapshot-on-shutdown`` example must be followed by a
        non-flag path token."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        violations: list[str] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "--snapshot-on-shutdown" not in line:
                continue
            # Find the token after --snapshot-on-shutdown
            idx = line.find("--snapshot-on-shutdown")
            rest = line[idx + len("--snapshot-on-shutdown") :].strip()
            # The next token should exist and not start with '-'
            next_token = rest.split()[0] if rest.split() else ""
            if not next_token or next_token.startswith("-"):
                violations.append(f"line {lineno}: {line.strip()}")
        assert not violations, (
            "live-validation/matrix-meshtastic.md has --snapshot-on-shutdown "
            "without a following path argument. Every usage must specify the "
            "snapshot output path (e.g. /tmp/medre-live-snapshot.json). "
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# 24. Bring-up runbook: diagnostics --refresh-health description
# ===========================================================================


class TestBringupRunbookDiagnosticsDescription:
    """The bring-up runbook must correctly describe ``diagnostics
    --refresh-health`` as starting a short-lived runtime itself, not
    requiring an already-running runtime."""

    def test_diagnostics_refresh_starts_runtime(self) -> None:
        """bringup docs must say --refresh-health starts a short-lived
        runtime itself and does not require an already-running runtime."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        lower = text.lower()
        # Must mention --refresh-health
        assert "--refresh-health" in text, (
            "live-validation/matrix-meshtastic.md must mention "
            "--refresh-health in the diagnostics section."
        )
        # Must say it starts a short-lived runtime
        assert "short-lived" in lower, (
            "live-validation/matrix-meshtastic.md must describe "
            "diagnostics --refresh-health as starting a 'short-lived' "
            "runtime, not requiring an already-running runtime."
        )
        # Must explicitly say it does NOT require an already-running runtime.
        # Strip markdown bold markers for matching.
        cleaned = lower.replace("**", "")
        assert "does not require" in cleaned or "not require" in cleaned, (
            "live-validation/matrix-meshtastic.md must explicitly state that "
            "diagnostics --refresh-health does not require an already-running "
            "runtime."
        )


# ===========================================================================
# 25. Bring-up runbook: targeting fields documented
# ===========================================================================


class TestBringupRunbookTargetingFields:
    """The bring-up runbook must document the four targeting fields
    (``source_room``, ``dest_room``, ``source_channel``, ``dest_channel``)
    used by routes to select source and destination."""

    @pytest.mark.parametrize(
        "field",
        ["source_room", "dest_room", "source_channel", "dest_channel"],
    )
    def test_targeting_field_documented(self, field: str) -> None:
        """bringup runbook must mention the targeting field."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        assert field in text, (
            f"live-validation/matrix-meshtastic.md must mention '{field}' "
            f"in the route targeting fields section."
        )


# ===========================================================================
# 26. Secure credentials bearer-token guidance
# ===========================================================================


class TestSecureCredentialsBearerToken:
    """configuration.md must provide complete guidance for bearer-token
    handling: file permissions, anti-leakage, rotation, dedicated accounts,
    and the auth command."""

    def test_mentions_chmod_600(self) -> None:
        """configuration.md must mention chmod 600 for config files."""
        if not _SECURE_CREDS.exists():
            pytest.skip("configuration.md not found")
        text = _read(_SECURE_CREDS)
        assert "chmod 600" in text, (
            "configuration.md must mention 'chmod 600' for protecting "
            "config files containing bearer tokens."
        )

    def test_mentions_never_paste_or_commit(self) -> None:
        """configuration.md must warn against pasting or committing
        tokens."""
        if not _SECURE_CREDS.exists():
            pytest.skip("configuration.md not found")
        text = _read(_SECURE_CREDS)
        lower = text.lower()
        assert "never" in lower and ("paste" in lower or "commit" in lower), (
            "configuration.md must warn against pasting or committing "
            "tokens (e.g. 'never paste', 'never commit')."
        )

    def test_mentions_rotate_or_rotation(self) -> None:
        """configuration.md must mention token rotation."""
        if not _SECURE_CREDS.exists():
            pytest.skip("configuration.md not found")
        text = _read(_SECURE_CREDS)
        lower = text.lower()
        assert "rotate" in lower or "rotation" in lower, (
            "configuration.md must mention token rotation "
            "(e.g. 'rotate', 'rotation')."
        )

    def test_mentions_dedicated_bot(self) -> None:
        """configuration.md must recommend using a dedicated bot
        account."""
        if not _SECURE_CREDS.exists():
            pytest.skip("configuration.md not found")
        text = _read(_SECURE_CREDS)
        lower = text.lower()
        assert "dedicated" in lower and "bot" in lower, (
            "configuration.md must recommend using a dedicated bot "
            "account for MEDRE."
        )

    def test_mentions_auth_command(self) -> None:
        """configuration.md must reference the auth CLI command."""
        if not _SECURE_CREDS.exists():
            pytest.skip("configuration.md not found")
        text = _read(_SECURE_CREDS)
        assert "medre adapter matrix auth login" in text, (
            "configuration.md must mention 'medre adapter matrix auth login' "
            "as the recommended way to populate tokens."
        )


# ===========================================================================
# 27. Operator answerability
# ===========================================================================


class TestOperatorAnswerability:
    """An operator should be able to answer fundamental operational questions
    from the bring-up runbook and example config docs alone.  These tests
    verify that the documentation covers each answerable question."""

    def test_which_config(self) -> None:
        """bringup doc must mention the example config path and the runtime
        config path."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        assert "examples/configs/live-matrix-meshtastic.toml" in text, (
            "bringup runbook must mention "
            "'examples/configs/live-matrix-meshtastic.toml' as the "
            "starting config template."
        )
        assert "/tmp/medre-live.toml" in text, (
            "bringup runbook must mention '/tmp/medre-live.toml' as the "
            "runtime config path."
        )

    def test_how_token(self) -> None:
        """bringup doc must describe how to obtain a Matrix token."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        assert "medre adapter matrix auth login" in text, (
            "bringup runbook must explain how to obtain a Matrix access "
            "token via 'medre adapter matrix auth login'."
        )

    def test_where_room_id(self) -> None:
        """bringup or example config docs must mention room_allowlist and
        room ID format."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        assert "room_allowlist" in text, (
            "bringup runbook must mention 'room_allowlist' for configuring "
            "which Matrix rooms the adapter listens on."
        )
        # Room ID format: !opaque:server
        has_room_format = (
            "!room" in text
            or "!abc" in text
            or re.search(r"!\w+:\w+", text) is not None
        )
        assert has_room_format, (
            "bringup runbook must show Matrix room ID format "
            "(e.g. '!room:example.com', '!abc123:example.com')."
        )

    def test_where_channel(self) -> None:
        """bringup or example config docs must mention channel index and
        channel 0."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        lower = text.lower()
        assert "channel" in lower, (
            "bringup runbook must mention 'channel' for Meshtastic "
            "channel configuration."
        )
        # Must reference channel index "0" specifically
        assert '"0"' in text, (
            'bringup runbook must show channel index "0" as an example '
            "Meshtastic channel configuration value."
        )

    def test_how_matrix_to_meshtastic_first(self) -> None:
        """bringup doc must present Matrix -> Meshtastic as the primary/first
        path."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        # Must have a section about Matrix -> Meshtastic
        assert (
            "Matrix" in text and "Meshtastic" in text
        ), "bringup runbook must mention both Matrix and Meshtastic."
        # Matrix -> Meshtastic section must appear before Meshtastic -> Matrix
        text.find("Matrix")
        text.find("Meshtastic")
        # The first section should be Matrix -> Meshtastic
        assert "primary" in text.lower() or "first" in text.lower(), (
            "bringup runbook must describe the Matrix -> Meshtastic direction "
            "as the primary or first path."
        )

    def test_how_inspect(self) -> None:
        """bringup doc must mention medre inspect receipts and
        medre inspect event."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        assert "medre inspect receipts" in text, (
            "bringup runbook must mention 'medre inspect receipts' for "
            "verifying delivery after a bridge run."
        )
        assert "medre inspect event" in text, (
            "bringup runbook must mention 'medre inspect event' for "
            "inspecting individual event timelines."
        )

    def test_what_sent_means(self) -> None:
        """bringup doc must explain that sent/success means local adapter
        or radio acceptance, not final remote receipt."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        lower = text.lower()
        # Must explain that success means local acceptance
        assert "local" in lower and ("radio" in lower or "adapter" in lower), (
            "bringup runbook must explain that success/sent means the "
            "local radio or adapter accepted the packet, not remote receipt."
        )

    def test_what_unproven(self) -> None:
        """bringup doc must mention Meshtastic -> Matrix as higher risk
        or unproven."""
        if not _BRINGUP.exists():
            pytest.skip("bringup runbook not found")
        text = _read(_BRINGUP)
        lower = text.lower()
        assert "higher risk" in lower or "unproven" in lower, (
            "bringup runbook must describe the Meshtastic -> Matrix "
            "direction as higher risk or unproven."
        )
