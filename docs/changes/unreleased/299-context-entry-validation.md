# Guided context pairs are validated at entry

Interactive `medre config bridge` drafts the accumulated bridge after each
source/destination context pair, so provider restrictions — allowlist
omissions, duplicate source contexts, pairs an existing route already
connects — report `Context rejected: <reason>` and re-prompt for the source
context instead of aborting the session after all prompts. Empty or
whitespace-padded context entry is rejected with an explicit message before
drafting. Structural request checks (unknown or disabled endpoints,
identical endpoints, existing route IDs) live in
`medre.runtime.bridge_setup.precheck_bridge` and run before the first
context prompt; the noninteractive `--map` path keeps failing at final
validation with exit 2.
