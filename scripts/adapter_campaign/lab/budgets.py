"""Outer orchestration budgets contain the sequential per-provider budgets."""

PROVIDER_CONTRACT_SECONDS = 1800
NESTED_STAGE_SECONDS = 2400
MARGIN_SECONDS = 300
NATIVE_STAGES = (
    "oauth",
    "provision",
    "hardware",
    "campaign",
    "resilience",
    "fidelity",
    "mmrelay",
    "recovery",
    "logs",
)


def stage_timeout(stage: str, provider_count: int) -> int:
    """Return an outer timeout in seconds, including orchestration margins.

    provider_count scales the contracts budget, including within "all".
    "native" covers all native stages; other names use a single-stage budget.
    """
    contracts = provider_count * PROVIDER_CONTRACT_SECONDS + MARGIN_SECONDS
    if stage == "contracts":
        return contracts
    if stage == "all":
        return contracts + len(NATIVE_STAGES) * NESTED_STAGE_SECONDS + MARGIN_SECONDS
    if stage == "native":
        return len(NATIVE_STAGES) * NESTED_STAGE_SECONDS + MARGIN_SECONDS
    return NESTED_STAGE_SECONDS + MARGIN_SECONDS
