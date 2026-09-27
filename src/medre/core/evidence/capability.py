"""Capability evidence derivation shared by reporting and evidence ledgers.

Delivery receipts persist the structured capability decision directly.  This
module derives the report-facing suppression reason from those structured
fields; no structure is ever recovered by parsing human-readable text.
"""

from __future__ import annotations

import re
from typing import TypedDict

#: Prefixes carried by current suppression error strings.  Stripped for
#: display only; nothing downstream parses the remaining text.
_SUPPRESSION_PREFIX_RE = re.compile(
    r"^(?:capability_suppressed|loop_suppressed|policy_suppressed):\s*"
)


class CapabilityEvidence(TypedDict):
    """Report-facing capability evidence for one delivery generation."""

    suppression_reason: str | None
    capability_field: str | None
    capability_level: str | None
    delivery_strategy: str | None


def derive_capability_evidence(
    error: str | None,
    failure_kind: str | None,
    status: str,
    *,
    capability_level: str | None = None,
    capability_field: str | None = None,
    capability_reason: str | None = None,
    delivery_strategy: str | None = None,
) -> CapabilityEvidence:
    """Derive capability evidence from the structured receipt fields.

    The structured fields pass through unchanged.  For suppressed receipts the
    suppression reason is the structured ``capability_reason`` when present;
    otherwise the suppression error text is shown with its ``<kind>: `` prefix
    stripped.
    """
    result: CapabilityEvidence = {
        "suppression_reason": None,
        "capability_field": capability_field,
        "capability_level": capability_level,
        "delivery_strategy": delivery_strategy,
    }
    suppressed = status == "suppressed" or failure_kind in {
        "capability_suppressed",
        "loop_suppressed",
        "policy_suppressed",
    }
    if suppressed and error:
        if capability_reason:
            result["suppression_reason"] = capability_reason
        else:
            stripped = _SUPPRESSION_PREFIX_RE.sub("", error, count=1).strip()
            result["suppression_reason"] = stripped or error
    return result
