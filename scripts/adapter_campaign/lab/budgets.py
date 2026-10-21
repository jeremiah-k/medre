"""Outer orchestration budgets contain the sequential per-provider budgets."""

PROVIDER_CONTRACT_SECONDS = 1800
NESTED_STAGE_SECONDS = 2400
MARGIN_SECONDS = 300


def stage_timeout(stage: str, provider_count: int) -> int:
    contracts = provider_count * PROVIDER_CONTRACT_SECONDS + MARGIN_SECONDS
    if stage == "contracts":
        return contracts
    if stage == "all":
        return contracts + 6 * NESTED_STAGE_SECONDS + MARGIN_SECONDS
    if stage == "native":
        return 6 * NESTED_STAGE_SECONDS + MARGIN_SECONDS
    return NESTED_STAGE_SECONDS + MARGIN_SECONDS
