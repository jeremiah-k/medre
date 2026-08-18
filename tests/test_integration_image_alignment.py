"""Guard against Docker integration image drift across integration surfaces.

The Synapse and meshtasticd images for the Docker integration tier are
referenced in several places that must stay aligned:

- ``docker-compose.integration.yaml`` — the source of truth (image@digest).
- ``.github/workflows/docker-integration.yml`` — CI pinned image@digest.
- ``tests/integration/conftest.py`` — tag-only fallback defaults.
- ``src/medre/runtime/docker_bridge_artifacts.py`` — tag-only env-fallback
  defaults.
- ``scripts/ci/run-docker-integration.sh`` — documented defaults in comments.

Renovate keeps every site aligned in a single update via the regex custom
managers in ``renovate.json``. This module statically scans the sites with
narrow, per-site patterns (never a generic image text search) and asserts
each agrees with the compose source of truth, which is read fresh at test
time — no hardcoded expected version, so an aligned bump never needs a
test edit. Tag-only defaults are intentional (local-run fallbacks); digest
assertions apply only where a site pins one. It is NOT docker-gated and
runs in the default suite, so drift fails fast at PR time rather than
silently diverging across CI / compose / local runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_COMPOSE = _REPO_ROOT / "docker-compose.integration.yaml"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docker-integration.yml"
_CONFTEST = _REPO_ROOT / "tests" / "integration" / "conftest.py"
_ARTIFACTS = _REPO_ROOT / "src" / "medre" / "runtime" / "docker_bridge_artifacts.py"
_RUN_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "run-docker-integration.sh"

# Central tag pattern: any run of characters that cannot continue an image
# reference — it stops at the ``@`` digest separator, whitespace, a closing
# quote (quoted Python defaults), or a closing paren (runner-script comment
# defaults), so it tolerates pre-release and other non-numeric tags
# (e.g. ``v1.159.0rc1``) rather than assuming dotted numerics.
_TAG = r"[^@\"'\s)]+"
_DIGEST = r"sha256:[0-9a-f]+"

# (label, image repo, environment variable name)
_IMAGES = [
    ("synapse", "matrixdotorg/synapse", "MEDRE_SYNAPSE_IMAGE"),
    ("meshtasticd", "meshtastic/meshtasticd", "MEDRE_MESHTASTICD_IMAGE"),
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compose_ref(repo: str) -> tuple[str, str | None]:
    """Return (tag, digest or None) for *repo* from the compose source of truth."""
    match = re.search(
        rf"image:\s*{re.escape(repo)}:({_TAG})(?:@({_DIGEST}))?",
        _read(_COMPOSE),
    )
    assert match is not None, f"{repo} image line not found in compose"
    return match.group(1), match.group(2)


def test_compose_pins_both_images_with_digest() -> None:
    """The source of truth pins a full image@digest for every image."""
    for _label, repo, _env in _IMAGES:
        _tag, digest = _compose_ref(repo)
        assert digest is not None, f"{repo} must be digest-pinned in compose"


@pytest.mark.parametrize(("label", "repo", "env"), _IMAGES)
def test_workflow_matches_compose(label: str, repo: str, env: str) -> None:
    """CI workflow pins the same tag and digest as compose.

    The workflow must pin the digest (not tag-only) — it is the reference CI
    actually runs, and compose is digest-pinned for deterministic behavior.
    """
    tag, digest = _compose_ref(repo)
    match = re.search(
        rf"{env}:\s*{re.escape(repo)}:({_TAG})(?:@({_DIGEST}))?",
        _read(_WORKFLOW),
    )
    assert match is not None, f"{env} not found in workflow"
    assert match.group(1) == tag, (
        f"workflow {label} tag drifted: compose is {tag}, "
        f"workflow is {match.group(1)}"
    )
    assert match.group(2) == digest, (
        f"workflow {label} digest drifted: compose is {digest}, "
        f"workflow is {match.group(2)}"
    )


@pytest.mark.parametrize(("label", "repo", "env"), _IMAGES)
def test_conftest_default_tag_matches_compose(
    label: str, repo: str, env: str
) -> None:
    """conftest fallback default carries the compose tag.

    Digest is intentionally omitted here: this default fires only for local
    runs without the image env var set, while CI and compose always pin the
    full image@digest. Tag-only keeps local pulls working without forcing a
    specific digest the local daemon may not have.
    """
    tag, _digest = _compose_ref(repo)
    matches = re.findall(
        rf'["\']{env}["\']\s*,\s*["\']{re.escape(repo)}:({_TAG})["\']',
        _read(_CONFTEST),
    )
    assert matches, f"no {env} fallback default found in conftest"
    drifted = [value for value in matches if value != tag]
    assert not drifted, (
        f"conftest {label} tag drifted: compose is {tag}, conftest is {drifted}"
    )


@pytest.mark.parametrize(("label", "repo", "env"), _IMAGES)
def test_artifacts_defaults_tag_matches_compose(
    label: str, repo: str, env: str
) -> None:
    """docker_bridge_artifacts env-fallback defaults carry the compose tag.

    All fallback sites (the evidence container/daemon field and the
    config-snapshot image field) read the image env var and fall back to a
    tag-only default. Real CI runs always set the env var to a pinned
    image@digest; the tag-only default matches conftest's rationale. Every
    site present must agree with compose; the occurrence count is
    intentionally unconstrained so adding a fallback site needs no test
    edit.
    """
    tag, _digest = _compose_ref(repo)
    matches = re.findall(
        rf'["\']{env}["\']\s*,\s*["\']{re.escape(repo)}:({_TAG})["\']',
        _read(_ARTIFACTS),
    )
    assert matches, f"no {env} fallback defaults found in docker_bridge_artifacts"
    drifted = [value for value in matches if value != tag]
    assert not drifted, (
        f"docker_bridge_artifacts {label} tag drifted: compose is {tag}, "
        f"got {drifted}"
    )


@pytest.mark.parametrize(("label", "repo", "env"), _IMAGES)
def test_run_script_comment_matches_compose(
    label: str, repo: str, env: str
) -> None:
    """The runner script's documented default carries the compose tag."""
    tag, _digest = _compose_ref(repo)
    match = re.search(
        rf"{env}\b[^\n]*?default:\s*{re.escape(repo)}:({_TAG})",
        _read(_RUN_SCRIPT),
    )
    assert match is not None, f"{env} default comment not found in script"
    assert match.group(1) == tag, (
        f"script {label} tag drifted: compose is {tag}, "
        f"script is {match.group(1)}"
    )
