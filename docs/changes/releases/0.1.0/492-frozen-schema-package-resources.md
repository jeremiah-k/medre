# Frozen schemas in package artifacts

Core source archives now include the canonical schema directory. Wheels include
the frozen schemas and freeze manifest as `medre/_schemas` resources,
so installed consumers can inspect the frozen contract without a source checkout.
The installed-package proof checks the packaged schema inventory and digests.
Example fixtures remain in the sdist for documentation and tests, but do not ship in
the wheel.

Require the core schema bundle at its documented medre/_schemas resource path
when verifying wheels. Support bundles report the packaged schema resources
when the source schema tree is unavailable; validator scripts remain an
independent source-only resource.
