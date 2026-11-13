# Retire the interop module

Delete `src/medre/interop/` from core: the `medre.interop.mmrelay`
compatibility module and its package go away entirely, with no alias left
behind. The last adapter import left the tree at the pinned Meshtastic
revision, and no adapter in the compatibility manifest imports the module.

The tests that exercised MEDRE's rendering of foreign MMRelay wire packets
keep their coverage with the wire-key literals inlined at their use sites:
the `medre.interop` namespace becomes a retired-namespace ban enforced by
the AST guard, the Matrix `displayname` trust boundary keeps its behavioral
coverage unchanged, and the architecture layer list drops the `interop`
entry.
