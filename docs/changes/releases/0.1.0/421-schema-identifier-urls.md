# Repository-owned schema identifier URLs

- Every schema `$id` under `docs/schemas/` moved from the uncontrolled
  `medre.dev` namespace to
  `https://raw.githubusercontent.com/jeremiah-k/medre/main/docs/schemas/<filename>`.
- The identifiers remain logical names for YAML language-server wiring; no
  code fetches them. Operators with editor mappings keyed on the old URLs
  must remap them to the repository URLs.
