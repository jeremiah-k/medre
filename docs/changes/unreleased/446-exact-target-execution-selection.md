# Exact-target execution selection at delivery admission

Replay execution accepts the same closed selection key as the typed
exact-target preview. `ReplayRequest.exact_target_selections` carries complete
`(event, plan, adapter, channel)` selections whose construction refuses
incomplete identities, the empty-string channel, non-selection elements, and
repeated identities; the empty tuple leaves delivery unrestricted and no
existing filter behavior changes.

When selections are present, a delivery plan reaches delivery admission only
when its full identity names a selection, and each named target is verified
against persisted outbox state before any generation is admitted or a send is
staged. The verification reuses `preview_exact_target`, so execution and the
read-only preview resolve the same target or both refuse for the same storage
state: an identity that is absent, contested across live generations, or
bound to a frozen staged plan contradicting the persisted row is refused and
no send is staged for it, while every other observed category proceeds to the
lifecycle's own outbox admission revalidation. Unselected plans are skipped
with the same semantics as `target_adapters` exclusion, sibling channels are
never widened, a drift refusal never falls back to re-planning, and storage
errors propagate under their own authority instead of becoming refusals.

Refusals surface as precise `exact_target_refused: <category> (<event>,
<plan>, <adapter>, <channel>)` strings on the deliver result, with per-target
records under `exact_target_refusals` in the result output when some selected
targets are admitted alongside them. The normative semantics live in
`docs/spec/exact-target-recovery.md` under "Execution selection at delivery
admission".
