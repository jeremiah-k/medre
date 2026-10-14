# External adapter packages

MEDRE no longer ships production Matrix, Meshtastic, MeshCore, or LXMF adapter
implementations in its core wheel. The registry discovers adapter specs from
installed `medre.adapters` entry points. Separately installed distributions
provide the unchanged transport-specific Python module paths. Configuration models, runtime wrappers, fakes, SDK dependencies, and protocol
tests now belong to those independent distributions. Core owns the generic
contracts, discovery, orchestration, routing, storage, evidence, and lifecycle.

After pulling the extraction, ignored `__pycache__` directories may remain
under removed adapter paths. Python can treat these as empty namespace
packages, making import errors confusing. Remove only the stale cache
directories under the removed paths after checking their contents; do not use
an indiscriminate `git clean -xdf`, which also deletes environments and local
state. Installed entry points remain the authority for provider availability.
