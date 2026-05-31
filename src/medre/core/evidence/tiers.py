"""Evidence tier classification for honest, machine-readable tier labels.

Evidence tiers describe *how* the evidence was produced, not what it claims.
Conservative inference ensures that storage-only, synthetic, or replay evidence
is never mislabelled as ``live_service`` or ``hardware``.

Tier hierarchy (weakest → strongest provenance)
------------------------------------------------
* ``synthetic``    — fake adapters, test fixtures, or no live systems involved.
* ``conformance``  — conformance / regression test suites with controlled inputs.
* ``docker``       — evidence collected inside a Docker bridge-artifact run
                     (explicit docker marker or artifact path present).
* ``live_service`` — evidence collected from a running service against real
                     transport adapters.  **Never inferred** — must be
                     explicitly provided.
* ``hardware``     — evidence collected against physical hardware.  **Never
                     inferred** — must be explicitly provided.

Design constraints
------------------
* Pure / deterministic — no I/O, no clocks, no hidden state.
* Conservative default — ``synthetic`` when provenance is ambiguous.
* JSON-safe — all values are plain strings.
* Unset sentinel — ``""`` (empty string) represents "unknown / not set".
"""

from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_TIER_UNKNOWN: str = ""
"""Unset sentinel — '' represents unknown / not set."""


# ---------------------------------------------------------------------------
# EvidenceTier enum
# ---------------------------------------------------------------------------


class EvidenceTier(str, Enum):
    """Machine-readable evidence provenance tier.

    Values are plain strings so ``json.dumps()`` succeeds without a custom
    encoder and ``msgspec`` can serialise them natively.
    """

    SYNTHETIC = "synthetic"
    CONFORMANCE = "conformance"
    DOCKER = "docker"
    LIVE_SERVICE = "live_service"
    HARDWARE = "hardware"


# ---------------------------------------------------------------------------
# Tier inference
# ---------------------------------------------------------------------------


def infer_evidence_tier(
    *,
    sources_seen: tuple[str, ...] | list[str] = (),
    adapter_kind: str | None = None,
    is_docker_artifact: bool = False,
    explicit_tier: str | None = None,
    source_adapter: str | None = None,
) -> str:
    """Conservatively infer the evidence tier from available signals.

    Inference priority (first match wins):

    1. *explicit_tier* is a valid tier value → use it.
    2. *adapter_kind* is ``"fake"`` → ``"synthetic"``.
    3. Any source in *sources_seen* is ``"replay"`` → ``"synthetic"``
       (replay is derived / synthetic by nature).
    4. *is_docker_artifact* is ``True`` → ``"docker"``.
    5. Default → ``"synthetic"`` (most conservative).

    **Important:** This function will **never** return ``"live_service"``
    or ``"hardware"`` unless *explicit_tier* is set to one of those values.
    Real adapter_kind alone is not sufficient to claim live or hardware
    provenance.

    Parameters
    ----------
    sources_seen:
        Distinct ``source`` values from delivery receipts (e.g. ``"live"``,
        ``"replay"``).
    adapter_kind:
        Adapter kind string (``"real"``, ``"fake"``, or ``None``).
    is_docker_artifact:
        Whether the evidence was collected from a Docker bridge-artifact run.
    explicit_tier:
        Caller-provided tier value.  When a valid :class:`EvidenceTier` value,
        returned as-is.
    source_adapter:
        Source adapter identifier (e.g. ``"fake_transport"``).  Used as a
        heuristic: names starting with ``"fake_"`` imply synthetic evidence.

    Returns
    -------
    str
        One of the :class:`EvidenceTier` value strings, or ``""`` for unknown.
    """
    # Priority 1: explicit tier.
    if explicit_tier is not None:
        try:
            return EvidenceTier(explicit_tier).value
        except ValueError:
            # Unknown explicit value — ignore and fall through.
            pass

    # Priority 2: fake adapter kind → synthetic.
    if adapter_kind == "fake":
        return EvidenceTier.SYNTHETIC.value

    # Priority 2b: fake source_adapter name heuristic.
    if source_adapter is not None and source_adapter.startswith("fake_"):
        return EvidenceTier.SYNTHETIC.value

    # Priority 3: replay source → synthetic.
    if "replay" in sources_seen:
        return EvidenceTier.SYNTHETIC.value

    # Priority 4: docker artifact → docker.
    if is_docker_artifact:
        return EvidenceTier.DOCKER.value

    # Priority 5: default — synthetic (most conservative).
    return EvidenceTier.SYNTHETIC.value


def tier_is_live(tier: str) -> bool:
    """Return ``True`` if the tier implies a live or hardware provenance."""
    return tier in (
        EvidenceTier.LIVE_SERVICE.value,
        EvidenceTier.HARDWARE.value,
    )
