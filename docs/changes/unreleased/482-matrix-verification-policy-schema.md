# Declare Matrix verification policy in the component schema

The Matrix adapter component schema accepts `verification_policy` with the
provider's `permissive` default and the closed `permissive`/`strict` values.
This aligns editor validation with the provider's typed configuration; it
neither establishes local device trust nor qualifies strict encrypted delivery.
