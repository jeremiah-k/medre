# Source-agnostic adapter installation preflight

Add `medre adapters check --config` to report declared adapter transports
missing an installed `medre.adapters` entry point. This check runs before
adapter-specific config parsing, never imports optional transport SDKs, and
never clones or installs packages. Document PyPI, pinned Git sources, local
editable paths, and locked deployment projects; align independently packaged
Meshtastic/LXMF adapter dependency pins with the current core development
baseline. Ordinary runtime startup remains side-effect-free for installation.
