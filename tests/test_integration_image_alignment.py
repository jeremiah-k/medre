"""Guard against Synapse Docker image drift across integration surfaces.

The Synapse image is referenced in several places that must stay aligned:

- ``docker-compose.integration.yaml`` — the source of truth (pinned image@digest).
- ``.github/workflows/docker-integration.yml`` — CI pinned image@digest.
- ``tests/integration/conftest.py`` — tag-only fallback default.
- ``src/medre/runtime/docker_bridge_artifacts.py`` — tag-only env-fallback defaults.
- ``scripts/ci/run-docker-integration.sh`` — documented default in a comment.

This module statically scans those files with narrow, per-site regexes (never
a generic ``synapse`` text search) and asserts the tag (and digest, where the
site pins one) agrees with the compose source of truth. It is NOT docker-gated
and runs in the default suite, so drift fails fast at PR time rather than
silently diverging across CI / compose / local runs.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_COMPOSE = _REPO_ROOT / "docker-compose.integration.yaml"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docker-integration.yml"
_CONFTEST = _REPO_ROOT / "tests" / "integration" / "conftest.py"
_ARTIFACTS = _REPO_ROOT / "src" / "medre" / "runtime" / "docker_bridge_artifacts.py"
_RUN_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "run-docker-integration.sh"

# Source of truth — must match docker-compose.integration.yaml exactly.
EXPECTED_TAG = "v1.155.0"
EXPECTED_DIGEST = "sha256:a87d002fba8efba807af19a876f488f4a9d298d6b62f5bab66d14e311a355e99"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_compose_is_source_of_truth() -> None:
    """Compose carries the canonical tag+digest everything else aligns to."""
    text = _read(_COMPOSE)
    # Match the synapse service image line specifically (meshtasticd also has
    # an image line on the same file, so anchor on the synapse repo).
    match = re.search(
        r"image:\s*matrixdotorg/synapse:(v[0-9.]+)@(sha256:[0-9a-f]+)",
        text,
    )
    assert match is not None, "synapse image line not found in compose"
    assert match.group(1) == EXPECTED_TAG, (
        f"compose tag drifted: expected {EXPECTED_TAG}, got {match.group(1)}"
    )
    assert match.group(2) == EXPECTED_DIGEST, (
        f"compose digest drifted: expected {EXPECTED_DIGEST}, got {match.group(2)}"
    )


def test_workflow_matches_source_of_truth() -> None:
    """CI workflow pins the same tag+digest as compose."""
    text = _read(_WORKFLOW)
    match = re.search(
        r"MEDRE_SYNAPSE_IMAGE:\s*matrixdotorg/synapse:(v[0-9.]+)@(sha256:[0-9a-f]+)",
        text,
    )
    assert match is not None, "MEDRE_SYNAPSE_IMAGE not found in workflow"
    assert match.group(1) == EXPECTED_TAG, (
        f"workflow tag drifted: expected {EXPECTED_TAG}, got {match.group(1)}"
    )
    assert match.group(2) == EXPECTED_DIGEST, (
        f"workflow digest drifted: expected {EXPECTED_DIGEST}, got {match.group(2)}"
    )


def test_conftest_default_tag_matches() -> None:
    """conftest fallback default carries the canonical tag.

    Digest is intentionally omitted here: this default fires only for local
    runs without ``MEDRE_SYNAPSE_IMAGE`` set, while CI and compose always pin
    the full image@digest. Tag-only keeps local pulls working without forcing
    a specific digest the local daemon may not have.
    """
    text = _read(_CONFTEST)
    match = re.search(
        r'_SYNAPSE_IMAGE\s*=\s*os\.environ\.get\(\s*'
        r'["\']MEDRE_SYNAPSE_IMAGE["\']\s*,\s*'
        r'["\']matrixdotorg/synapse:(v[0-9.]+)["\']',
        text,
    )
    assert match is not None, "_SYNAPSE_IMAGE default not found in conftest"
    assert match.group(1) == EXPECTED_TAG, (
        f"conftest default tag drifted: expected {EXPECTED_TAG}, got {match.group(1)}"
    )


def test_artifacts_defaults_tag_matches() -> None:
    """docker_bridge_artifacts env-fallback defaults carry the canonical tag.

    Both fallback sites (the evidence container field and the config-snapshot
    synapse_image field) read MEDRE_SYNAPSE_IMAGE and fall back to a tag-only
    default. Real CI runs always set the env var to a pinned image@digest;
    the tag-only default matches conftest's rationale.
    """
    text = _read(_ARTIFACTS)
    matches = re.findall(
        r'["\']MEDRE_SYNAPSE_IMAGE["\']\s*,\s*["\']matrixdotorg/synapse:(v[0-9.]+)["\']',
        text,
    )
    assert len(matches) == 2, (
        f"expected 2 MEDRE_SYNAPSE_IMAGE fallback defaults in "
        f"docker_bridge_artifacts.py, found {len(matches)}"
    )
    drifted = [tag for tag in matches if tag != EXPECTED_TAG]
    assert not drifted, (
        f"docker_bridge_artifacts default tag drifted: expected "
        f"{EXPECTED_TAG}, got {drifted}"
    )


def test_run_script_comment_matches() -> None:
    """The runner script's documented default matches the canonical tag."""
    text = _read(_RUN_SCRIPT)
    match = re.search(
        r"MEDRE_SYNAPSE_IMAGE\b[^\n]*?"
        r"default:\s*matrixdotorg/synapse:(v[0-9.]+)",
        text,
    )
    assert match is not None, "MEDRE_SYNAPSE_IMAGE default comment not found in script"
    assert match.group(1) == EXPECTED_TAG, (
        f"script comment tag drifted: expected {EXPECTED_TAG}, got {match.group(1)}"
    )
