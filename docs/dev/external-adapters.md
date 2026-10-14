# External adapter extraction (first migration)

MEDRE discovers installed transports from the `medre.adapters` Python entry-point
namespace. External adapter packages provide a zero-argument `adapter_spec()`
factory returning `AdapterSpec`, and their source retains the original
`medre.adapters.<transport>` paths using namespace-path extension. No SDK
is downloaded by MEDRE startup.

The canonical standalone distributions are:

- `medre-adapter-matrix`
- `medre-adapter-meshtastic`
- `medre-adapter-meshcore`
- `medre-adapter-lxmf`

Install MEDRE and desired adapter distribution(s) in the same environment.
Installing packages after the interpreter has imported the registry requires a
restart. Missing adapters are not silently installed; configuration validation
reports unknown transports. Core retains the adapter configuration models,
test fakes, and historical SDK convenience extras; move those to the adapter
distributions together with the uv lockfile update as the packaging separation
completes. Production adapter implementations and their registration live in
separately installed package entry points. Do not publish core+adapter
distributions as final until the remaining config and extras separation is
complete and validated.

Git repositories are independent and maintain their full source history.
The extraction commits have separate migration identity; the original
historical author and committer fields were not rewritten.

The LXMF/RNS license conditions require independent legal review; creating
a separately installable Python package is _not_ a legal isolation boundary.

## Distribution sources (pre-release design)

MEDRE's entry-point system deliberately treats Git, PyPI, a private index,
and a local editable path as equivalent **installation sources**. Bridge YAML
must not contain installer commands or Git URLs. Put reviewed dependencies in
an operator deployment project (`pyproject.toml`, `tool.uv.sources` and
`uv.lock`) or a pinned pip requirements file, and use
`medre adapters check --config` before the full config validation.

The standalone adapter packages, **not core extras**, are the long-term owner
of SDK constraints. The old core SDK extras remain temporarily and must be
removed (with lockfile/CI updates) before the first release. Cross-package pin
skew is a resolver failure, not something the runtime should silently repair.

See [installation](../ops/adapter-installation.md) for PyPI, Git and local
checkout instructions, source locking, and the security boundary.
