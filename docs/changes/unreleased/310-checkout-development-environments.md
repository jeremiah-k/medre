# Checkout development environments

- Add `medre dev sync` to synchronize locked core dependencies and the CI-pinned
  SDK-free adapter set with one command, independent of sibling checkout layouts.
- Support explicit SDK resolution constrained by the core lock, source catalogs,
  arbitrarily located editable providers, interpreter selection and dry-run output.
- Read ignored `.medre-dev.yaml` preferences without editing tracked dependency
  files or compatibility pins. Validate selections before mutating the environment.
- Add `medre dev test` to run an editable adapter's own pytest suite with the
  checkout interpreter and adapter working directory. Preserve Windows pytest
  exit status and optionally record the selected checkout's Git evidence.
