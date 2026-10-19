# Chatmail and SimpleX compatibility qualification

Include the merged Chatmail and SimpleX Chat providers in the curated CI and
developer snapshot. Run their owned unit/native SDK contracts on all supported
Python versions, and prepare the official SimpleX native release in a private
runner cache before starting its native tier. Runtime discovery stays based on
installed provider entry points.

Preserve established native-detection precedence without assuming a closed set
of installed providers.

Chatmail and SimpleX transport profiles publish the complete capability
declarations. Conformance checks use typed provider samples so configured
default text budgets agree between real and fake adapters, including XMPP.
Sample identifier checks follow the discovered provider set.
