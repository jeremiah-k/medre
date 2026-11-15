# Produce the policy content ceiling at admission

A `content_ceiling` configuration section declares one bounded hard content
privacy ceiling; an omitted or disabled section produces no content deadline
and leaves every ceiling gate dormant, and enabling without a declared
window is refused at load time.

When enabled, durable admission derives each event's absolute UTC ceiling
from the trusted local admission time plus the declared window and commits
it inside the admission transaction into both content rows: the mandatory
`event_content_dependencies.deadline` and the immutable
`event_content_access_policies.hard_ceiling` row, installed with an empty
consumer allowlist and an optional-hold bound spanning the admitted window.
An admission whose deadline disagrees with an installed ceiling fails closed
with the typed `ContentCeilingMismatchError` and rolls back; duplicate
admissions observe the installed ceiling unchanged.

The outbox adoption fence enforces the effective deadline as the earlier of
the source dependency deadline, the immutable event ceiling, and the frozen
delivery-plan deadline, which rides the outbox creation transaction from its
dispatch snapshot. A generation staged after the effective deadline adopts
no content dependency and remains an auditable obligation, and an event
governed by an installed ceiling refuses `append` dependencies that carry no
deadline. With no deadline, no ceiling row, and no frozen plan deadline,
admission and adoption behave exactly as before.

The strictly fenced pending-outbox cleanup transition and the
native-submit fence qualification remain separate work.
