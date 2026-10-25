# Observe native XMPP attribution independently

Let targeted XMPP campaign endpoints require structured relay context. Record
claims from the independent peer's native XML and check them against authored
sender/origin fixtures and canonical ingress references, both during execution
and retained-evidence analysis. Existing baseline-v1 defaults remain unchanged.

Refresh compatibility selections to the merged Matrix and LXMF shared-context
implementations. The XMPP candidate remains an explicit targeted selection until
its own PR merges.

The XMPP-inclusive offline analyzer now requires the independently authored
campaign plan. It checks required context in smoke as well as baseline runs,
compares compact sender attribution when supplied, and refuses retained report
requirement downgrades. Shared campaign fixture builders live in `tests.helpers`
so the test suite's module-isolation rule stays intact.
