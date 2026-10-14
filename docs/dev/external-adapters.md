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
reports unknown transports. Adapter config models, test fakes, and historical
SDK convenience extras remain inside MEDRE **temporarily** during this staged
migration. Move those to adapters and update the uv lockfile in the next
tranche before the first public release. The present change only relocates
production adapter implementations and moves registration to installed package
entry points. Do not publish core+adapter distributions as final until the
remaining config and extras separation is complete and validated.

Git repositories are independent and maintain their full source history.
The extraction commits have separate migration identity; the original
historical author and committer fields were not rewritten.

The LXMF/RNS license conditions require independent legal review; creating
a separately installable Python package is *not* a legal isolation boundary.
