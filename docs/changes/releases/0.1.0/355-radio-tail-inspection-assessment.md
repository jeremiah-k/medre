# Explain retained radio continuation boundaries during inspection

Extend read-only `medre inspect radio-attempt` with `--assess-continuation`.
The optional report distinguishes review candidates with accepted-prefix and
unsent-tail evidence from uncertain, legacy or completed attempts. It identifies
missing local payload proof, inconsistent accepted-part fingerprints and
channel-scope disagreement, including explicit native channel IDs that contradict
the retained target, without attempting native submission, recovering a pending part, or changing SQLite.

A candidate still needs a new independently owned outbox claim and a complete
native-semantic check under the established continuation transaction. This
inspection capability is not automatic replay or recipient acknowledgement.
