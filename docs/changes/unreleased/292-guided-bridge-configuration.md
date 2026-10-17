# Guide bridge configuration with offline route preview

Add `medre config bridge` to select two configured adapter instances, author
explicit conversation mappings, choose direction and attribution, and review
production route expansion before saving a new private YAML file. Without an
input file, start from installed providers' fake samples. Preserve provider
settings, raw secrets and path placeholders; do not copy environment values
into YAML or start adapters. Reject invalid plans and duplicate explicit text
bridges; never overwrite an existing output path.

Add `medre smoke --source-context` for explicit fake conversation coverage.
Require the event factory to preserve both adapter identity and the requested
context; do not forge provenance if a fake ignores the request. Report the
actual source context alongside its adapter. Context selection applies only to
normal smoke. Existing factories remain usable without the optional argument.

Redact known configuration/environment secret values from guided setup errors,
including provider-declared secret fields.
Cover ordinary strings, Python repr, and JSON escaping so multiline values
remain redacted when a provider formats them in an exception.
