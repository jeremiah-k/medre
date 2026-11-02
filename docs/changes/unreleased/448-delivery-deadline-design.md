# Design the delivery deadline and hard content ceiling

`docs/dev/delivery-deadline-hard-ceiling.md` inventories the deadline
machinery already on `main` — the mandatory dependency deadline, the outbox
adoption fence, the native-start fence and the dormant plan-deadline hooks —
and fixes the enforcement design for register row G03.

The design rides the existing content-authority rows and transactions rather
than adding an authority: the operator ceiling is written as the mandatory
dependency deadline at admission, planning produces the policy delivery
deadline that the pre-render and pre-handoff checks already read, and a
ceiling sweep retires unclaimed pending work through the existing guarded
terminal transitions. Reserved attempts and queued handoffs stay outside
cleanup, so an expired deadline never relabels accepted or uncertain native
work.

Developers implementing the deadline stack can dispatch against an anchored
inventory and frozen refusal semantics instead of re-deriving where the
checks belong.
