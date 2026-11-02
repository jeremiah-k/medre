# Design the delivery deadline and hard content ceiling

`docs/dev/delivery-deadline-hard-ceiling.md` inventories the deadline
machinery already on `main` — the mandatory dependency deadline, the outbox
adoption fence, the native-start fence and the dormant plan-deadline hooks —
and fixes the enforcement design for register row G03.

The design reuses existing content-authority rows and transactions: admission
must install the same immutable ceiling in availability policy and mandatory
dependencies, including append-owned content; planning freezes a separate
possibly earlier delivery deadline. Adoption and native submission enforce
the earlier effective limit. A strictly unstarted, unclaimed pending/retry
transition (which must be implemented) handles bounded cleanup; claimed,
in-progress, reserved and queued work stays with existing evidence authority.
No expired deadline may relabel accepted or uncertain native work.

Developers implementing the deadline stack can dispatch against an anchored
inventory and frozen refusal semantics instead of re-deriving where the
checks belong.
