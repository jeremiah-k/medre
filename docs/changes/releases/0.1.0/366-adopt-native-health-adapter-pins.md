# Adopt native-health adapter revisions and align SimpleX ownership wording

Adopt the seven merged adapter revisions in
`.github/adapter-compatibility.json`: matrix, meshtastic, meshcore, lxmf,
xmpp, chatmail, and simplexchat now pin their native-health implementation
heads. Extras and platform constraints are unchanged.

Correct the SimpleX transport profile's ownership claim to match the qualified
adapter behavior: a failed cleanup may retain fencing, because the kernel
releases the profile lock only after the last inherited handle closes, so a
surviving worker keeps the profile fenced. Leader exit alone is not proof of a
full native-owner exit; operators confirm no surviving worker before reusing a
profile.

Spell out the current-health notification properties the runtime already
implements and now tests: a report repeating the retained observation stays
silent, and each provider's entry keeps its own observation time.
