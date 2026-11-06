# Promote reviewed Matrix policy and provider setup journeys

Promote the merged Matrix, Chatmail, and SimpleX Chat adapter revisions in the
compatibility snapshot. Matrix configuration accepts `verification_policy`,
with `permissive` as the default and `strict` requiring affirmative local nio
recipient-device trust for encrypted sends. Publish the schema with the matching
provider pin, and align security, setup, and limitation documentation with the
opt-in Docker qualification and its local-only scope.

Chatmail and SimpleX Chat expose their typed setup journeys through core's
offline instructions renderer; provisioning remains operator-owned.
