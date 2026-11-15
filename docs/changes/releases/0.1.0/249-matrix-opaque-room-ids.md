# Matrix room version 12 auto-join support

Matrix configuration accepts domainless room IDs from room version 12 as well
as legacy IDs containing a server suffix. Auto-join targets and allowlists
preserve the exact case-sensitive ID returned by the homeserver. Bare `!`
and room aliases remain invalid auto-join targets.

The Docker encrypted-room checks accept either room-ID form, allowing current
Synapse defaults to exercise encrypted adapter startup and delivery.
