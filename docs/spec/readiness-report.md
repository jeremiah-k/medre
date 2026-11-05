# Offline readiness report

## Scope and authority

`medre readiness` composes the existing offline checks into one read-only
report. It is authoritative for exactly one thing: what offline checks can
establish about an installation and its configuration. It establishes
nothing else, and the report MUST NOT be read as native acceptance,
runtime readiness, or recipient verification.

The command performs no adapter SDK import, no login, no network or
hardware I/O, and no write of any kind: no storage write, no file write,
no configuration change. Its only output is stdout. Distribution presence
is read through `importlib.metadata` against the compatibility snapshot's
declared distribution names; provider modules are never probed or imported
by name, and the report never executes provider code outside the
provider-owned pure configuration preflight that guided authoring and
startup already share.

The composition reuses existing surfaces rather than duplicating them:
the compatibility snapshot
(`.github/adapter-compatibility.json`), the SDK-free adapter registry and
its quarantining discovery, config loading with environment overrides
(the same gate as `medre run`), the offline route-plan expansion, and the
provider-owned pure preflight (`prepare_adapter_config`). Detailed
diagnostics remain with the dedicated commands; the report names them in
its guidance instead of restating their output.

## Command surface

```
medre readiness [--config PATH] [--json]
```

The command exits 0 whenever it produces a report. A report that lists
missing providers, blocked adapters, or an invalid configuration is a
successful report: zero installed providers is a first-class outcome, not
an error. Exit codes do not encode readiness; consumers read the report
fields (or the exit-1 path for an unexpected internal failure).

## Report sections

The JSON report (`--json`) has a `report_version` (currently `1`), the
running `medre_version`, and the sections below. The machine contract is
[`readiness-report.schema.json`](../schemas/readiness-report.schema.json). There are no timestamps
and no filesystem paths anywhere in the report; identical input and
environment produce identical output.

### Manifest section

`manifest` reports the compatibility snapshot for the running
installation. Builds derive its packaged resource from the canonical
`.github/adapter-compatibility.json`; source distributions retain that canonical
input so rebuilding a wheel preserves the same pins. Editable installations
read their own source checkout when no packaged resource exists. A
working-directory checkout is never a fallback for a missing installed snapshot,
because its pins may describe a different build.
`status` is `available`, `not_found` (no snapshot is
reachable in the installation, including a file removed before reading),
or `malformed` (the snapshot exists but fails the structural
rules the development-environment authority applies: at least one entry,
a valid transport token, an owner/repository pair, a full commit SHA, a
distribution name, a unique transport alias, optional string extras and Python
requirements, and a Boolean local-integration flag). An unavailable or
malformed snapshot yields no provider rows; the report never fabricates
provider claims without the snapshot.

When available, every snapshot entry is reported in manifest order,
installed or not, so a missing provider stays visible:

| Field | Meaning |
| --- | --- |
| `transport`, `repository`, `ref`, `extras`, `distribution`, `python_requires`, `local_integration` | The snapshot's own declaration; `ref` and `distribution` make a report reproducible. |
| `distribution_status`, `installed_version` | `installed` or `missing` for the declared provider distribution, read from distribution metadata only. |
| `registration` | `registered` (healthy provider spec discovered), `broken` (quarantined by discovery), or `unregistered`. |
| `registration_error_category` | For `broken` only: a valid exception class name from the discovery diagnostic, or `provider_error` when no colon separator or valid exception-name prefix exists. Never the diagnostic text. |
| `native_setup` | `provider_guided` when the provider declares native setup fields (the pure offline setup form exists), `manual_provisioning` when the operator must follow the provider's own guide, `null` when no spec is available for inspection. |

An installed provider with no registered adapter entry point MUST produce
registration-repair guidance even when no adapter instance is configured.
It MUST NOT produce the complete-readiness guidance.

### Configuration section

`config` loads and environment-overrides the configuration exactly as
`medre run` does. `status` is `valid` or a typed error category:
`config_not_found`, `config_file_error`, `validation_error`,
`config_error`, or `unexpected_error`. Validation errors carry the
structural `section_path` (a redacted YAML key path; operator-chosen adapter
IDs, unregistered transport keys, route IDs and logger names are omitted); error text, config values,
and environment values never enter the report. The `source` field is the
original config-discovery source name, not a path or a second explicit
lookup of the resolved file.

### Adapter section

`adapters` lists one row per configured instance in config registration
order, when the configuration is valid. Each row answers only what
offline checks can know:

| Field | Meaning |
| --- | --- |
| `adapter_id`, `transport`, `enabled`, `adapter_kind` | The configured instance identity. |
| `provider_distribution`, `provider_distribution_status` | The snapshot's provider distribution for this transport and whether it is installed; `unknown` when no snapshot declares the transport. |
| `sdk_distribution`, `sdk_distribution_status`, `sdk_installed_version` | The provider-declared SDK distribution for `real` instances: `installed` or `missing`; `not_required` for `fake` instances and providers without a declared SDK; `unknown` when no spec is available. |
| `config` | `valid` or `actionable_error`. |
| `preflight`, `blocked_category` | `passed`, or `blocked` with one typed category: `transport_unregistered`, `transport_broken`, `config_invalid`, `sdk_distribution_missing`, or `preparation_failed`. |
| `native_setup` | As in the manifest section. |
| `runtime` | Always `not_observed`. |
| `recipient` | Always `not_observed`. |

The preflight runs the provider-owned pure configuration preparation
shared by authoring and startup, with the runtime route expansion the
builder uses. A failure is a typed category, never the provider's error
text.

### Routes section

`config.routes` summarizes the offline route-plan expansion: `status`
(`passed` or `blocked`), a typed `error_category`
(`route_expansion_error` or `route_conflict`), and route counts. If the
route-plan builder itself raises, the status is `blocked`, the category is
`route_expansion_error`, and `total`, `enabled`, `expansion_failures` and
`conflicts` are `null` because no plan was available to count. Per-route
detail stays with `medre routes plan`. When configuration is not loaded,
`routes` is `null`.

### Boundary section

`boundary` states the observation boundary in machine-readable form:
`offline` is always `true`, `runtime` and `recipients` are always
`not_observed` — the native-evidence vocabulary's explicit
absence-of-observation token — and `statement` carries the human-readable
boundary. The rule is the native-evidence contract's central rule applied
to preflight: absence of evidence never proves acceptance, delivery, or
rejection, and a passed preflight licenses exactly nothing offline checks
did not establish.

## Sanitization

The report MUST NOT contain credentials, secret values, raw SDK or
provider error text, native identifiers, message content, filesystem
paths, or timestamps. All failures reduce to the typed categories above.
Human guidance names commands and documentation paths, never operator
paths or values.

## Human output

Without `--json` the command prints a short, path-free summary: the
snapshot with per-provider installation and registration states, the
configuration outcome, per-adapter blocked reasons, the route-plan
outcome, the boundary statement, and ordered next-step guidance. The
guidance is actionable (which command or document to consult next) and
always ends with the boundary statement.
