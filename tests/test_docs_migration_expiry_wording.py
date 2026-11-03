"""Documentation-consistency guards for migration and privacy wording.

Cheap guardrails pinning two documentation reconciliations with merged
behavior, so the contradictions cannot quietly return:

- Merged #440 allows specific guarded additive pre-release migrations. The
  decision-audit spec pages must therefore distinguish the exact allowlisted
  additive predecessor (rebuilt transactionally at a writable open; a
  read-only open reports that a guarded writable migration is available)
  from unsupported catalogs (refused with recreation guidance), without
  claiming a blanket no-migration stance or a general upgrade framework.
- The operator getting-started guide must not claim message expiry is
  blanket-unavailable: narrow implemented mechanisms exist (opt-in
  relay-only logical collection, the event-level hard content ceiling, the
  default-off guarded retirement sweep), and the page must state their
  limits and link to the privacy and message storage guide.

Assertions match exact authored phrases after whitespace normalization so
prose rewrapping does not cause false failures, and the forbidden claims are
matched as exact phrases rather than broad regexes that could trip on
legitimate discussion elsewhere on a page.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

SPEC_DIR = _ROOT / "docs" / "spec"
DEV_DIR = _ROOT / "docs" / "dev"
OPS_DIR = _ROOT / "docs" / "ops"

EXACT_TARGET_DECISION_MD = SPEC_DIR / "exact-target-decision.md"
OPERATOR_EXACT_RESOLUTION_MD = SPEC_DIR / "operator-exact-resolution.md"
COMPATIBILITY_BOUNDARIES_MD = SPEC_DIR / "compatibility-boundaries.md"
GETTING_STARTED_MD = OPS_DIR / "getting-started.md"
MESSAGE_STORAGE_MD = OPS_DIR / "message-storage.md"
CONTENT_LIFECYCLE_MD = DEV_DIR / "content-lifecycle.md"
DEVELOPMENT_PRIORITIES_MD = DEV_DIR / "development-priorities.md"

#: Read-only phrasing the reconciled spec pages must carry verbatim (after
#: whitespace normalization) when describing the recognized predecessor.
_READONLY_REPORT_PHRASE = (
    "a read-only open of that recognized predecessor reports that a "
    "guarded writable migration is available"
)


def _normalized(path: Path) -> str:
    """Read a document and collapse all whitespace runs to single spaces.

    Prose is wrapped near 88 columns, so assertions are written against
    whitespace-normalized text; the phrases themselves stay exact.
    """
    return " ".join(path.read_text(encoding="utf-8").split())


# ===========================================================================
# 1. Decision-audit spec pages vs the allowlisted additive predecessor
# ===========================================================================


def test_exact_target_decision_names_allowlisted_predecessor() -> None:
    """The predecessor migration is allowlisted, not a general upgrade."""
    text = _normalized(EXACT_TARGET_DECISION_MD)
    assert (
        "The exact single-action predecessor layout is an allowlisted "
        "additive predecessor, not an unsupported catalog" in text
    ), (
        "exact-target-decision.md must state that the single-action "
        "predecessor layout is an allowlisted additive predecessor, "
        "distinct from unsupported catalogs"
    )


def test_exact_target_decision_describes_writable_and_readonly_opens() -> None:
    """Writable open migrates; read-only open reports the migration."""
    text = _normalized(EXACT_TARGET_DECISION_MD)
    assert (
        "a writable open rebuilds the table from the authoritative DDL "
        "inside the open transaction" in text
    ), (
        "exact-target-decision.md must describe the guarded transactional "
        "rebuild at a writable open"
    )
    assert _READONLY_REPORT_PHRASE in text, (
        "exact-target-decision.md must state that a read-only open of "
        "the recognized predecessor reports the available guarded "
        "writable migration"
    )


def test_exact_target_decision_keeps_fail_closed_refusal() -> None:
    """Unsupported older shapes stay refused with recreation guidance."""
    text = _normalized(EXACT_TARGET_DECISION_MD)
    assert (
        "Any other older shape outside the additive allowlist is still refused "
        "at initialization with recreation guidance" in text
    ), (
        "exact-target-decision.md must keep the fail-closed refusal for "
        "shapes outside the additive allowlist"
    )
    assert "no general upgrade framework" in text, (
        "exact-target-decision.md must state that no general upgrade "
        "framework is provided"
    )


def test_exact_target_decision_has_no_blanket_no_migration_claim() -> None:
    """The pre-#440 blanket refusal claims must not return."""
    text = _normalized(EXACT_TARGET_DECISION_MD)
    assert "no migration path is provided" not in text, (
        "exact-target-decision.md must not claim the known predecessor "
        "decision-table shape has no migration path"
    )
    assert "MEDRE refuses older single-action shapes at initialization" not in text, (
        "exact-target-decision.md must not blanket-refuse the recognized "
        "single-action predecessor shape"
    )


def test_operator_resolution_names_allowlisted_predecessor() -> None:
    """The single recognized exception is the allowlisted predecessor."""
    text = _normalized(OPERATOR_EXACT_RESOLUTION_MD)
    assert (
        "the exact single-action `operator_delivery_decisions` predecessor "
        "is one such allowed shape" in text
    ), (
        "operator-exact-resolution.md must name the decision-table predecessor "
        "as one of the guarded additive shapes"
    )
    assert _READONLY_REPORT_PHRASE in text, (
        "operator-exact-resolution.md must state that a read-only open "
        "of the recognized predecessor reports the available guarded "
        "writable migration"
    )


def test_global_compatibility_policy_names_guarded_additive_migration() -> None:
    """The general compatibility policy must not contradict storage authority."""
    text = _normalized(COMPATIBILITY_BOUNDARIES_MD)
    assert "no general automatic SQLite schema migration framework" in text
    assert "recognizes only structurally exact additive predecessors" in text
    assert "these can migrate transactionally without discarding their data" in text
    assert "A read-only inspection never performs the additive migration" in text
    assert "no automatic SQLite schema transformation" not in text


def test_operator_resolution_keeps_fail_closed_refusal() -> None:
    """Layouts outside the allowlist stay refused; no upgrade framework."""
    text = _normalized(OPERATOR_EXACT_RESOLUTION_MD)
    assert (
        "Any other earlier development layout MUST still be refused with "
        "recreation guidance" in text
    ), (
        "operator-exact-resolution.md must keep refusing earlier layouts "
        "outside the additive allowlist"
    )
    assert "not a general upgrade framework" in text, (
        "operator-exact-resolution.md must state the allowlist is not a "
        "general upgrade framework"
    )


def test_operator_resolution_has_no_blanket_no_upgrade_claim() -> None:
    """The pre-#440 blanket no-upgrade-path claim must not return."""
    text = _normalized(OPERATOR_EXACT_RESOLUTION_MD)
    assert "No upgrade path from earlier development layouts is provided" not in text, (
        "operator-exact-resolution.md must not blanket-claim that no "
        "upgrade path from earlier development layouts is provided"
    )


# ===========================================================================
# 2. Getting-started expiry wording vs the implemented narrow mechanisms
# ===========================================================================


def test_no_blanket_unavailable_expiry_claim() -> None:
    """The blanket claim must not return without the scoped qualifier."""
    text = _normalized(GETTING_STARTED_MD)
    assert "Automatic message expiry is not available yet" not in text, (
        "getting-started.md must not claim automatic message expiry is "
        "blanket unavailable"
    )


def test_states_implemented_narrow_mechanisms() -> None:
    """The page must name the narrow implemented mechanisms."""
    text = _normalized(GETTING_STARTED_MD)
    assert "an opt-in relay-only policy can logically collect" in text, (
        "getting-started.md must mention the opt-in relay-only logical "
        "collection policy"
    )
    assert (
        "an explicitly enabled hard content ceiling fences new local "
        "native dispatch starts when the deadline is observed" in text
    ), "getting-started.md must mention the explicit hard content ceiling"
    assert "work already accepted by a transport may still arrive later" in text, (
        "getting-started.md must not imply that a ceiling retracts work "
        "previously accepted by a transport"
    )
    assert (
        "The guarded retirement sweep for expired unstarted work is disabled "
        "by default" in text
    ), ("getting-started.md must mention the default-off guarded " "retirement sweep")


def test_states_limits_and_links_storage_guide() -> None:
    """The page must state what is not available and link the guide."""
    text = _normalized(GETTING_STARTED_MD)
    assert (
        "General timed retention profiles and physical erasure are not "
        "available" in text
    ), (
        "getting-started.md must state plainly that general timed "
        "retention profiles and physical erasure are not available"
    )
    assert "copies held by connected transports and providers are unaffected" in text, (
        "getting-started.md must state that provider-owned copies are " "unaffected"
    )
    raw = GETTING_STARTED_MD.read_text(encoding="utf-8")
    assert "[privacy and message storage](message-storage.md)" in raw, (
        "getting-started.md must link to the privacy and message storage " "guide"
    )
    assert MESSAGE_STORAGE_MD.is_file(), "message-storage.md must exist"


def test_opening_qualifies_general_expiry_against_narrow_collector() -> None:
    """The opening must scope the proposed design to general expiry."""
    text = _normalized(CONTENT_LIFECYCLE_MD)
    assert (
        "Retention profiles, optional plugin holds and general automatic "
        "expiry remain a proposed design" in text
    ), (
        "content-lifecycle.md must scope its proposed-design opening to "
        "general automatic expiry"
    )
    assert "The narrow implemented steps in that direction" in text, (
        "content-lifecycle.md must name the narrow implemented steps "
        "beside the proposed design"
    )
    assert (
        "Retention profiles, optional plugin holds and automatic expiry "
        "remain a proposed design" not in text
    ), (
        "content-lifecycle.md must not blanket-frame automatic expiry as "
        "proposed-only now that the narrow collector is implemented"
    )


def test_priorities_distinguishes_constraint_widening_from_narrowing() -> None:
    """The shared policy permits the exact widening the decision specs describe."""
    text = _normalized(DEVELOPMENT_PRIORITIES_MD)
    assert "Renames, altered constraints" not in text
    assert "constraint narrowing" in text
    assert "allowlisted constraint widenings follow the guarded additive policy" in text
    decision = _normalized(EXACT_TARGET_DECISION_MD)
    assert "PRAGMA foreign_key_check(operator_delivery_decisions)" in decision
