# Upgrade MEDRE and installed adapters through their owning tool environment

medre upgrade and medre update share one workflow. Upgrade core, all installed
adapters or selected provider distributions; preview every operation with
--dry-run. Scoped upgrades retain the complete tool request and use uv's
selective install flags instead of its bulk tool upgrade. Unselected floating
Git/local sources cause a scoped operation to refuse before mutation rather than
advancing unrelated code or persisting temporary pins. Third-party and broken
providers are identified from metadata without importing their implementations.

An explicit --sync-sources applies configured package requirements, including an
optional core source, while retaining unrelated requests and installed extras.
The source catalog includes all seven maintained adapters. Removed aliases can
be cleaned up by installed distribution name without deleting native state.
Other installation environments receive ownership-specific guidance. Windows
retains its explicit executable-lock handoff.
