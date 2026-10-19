# Align cross-transport tests with MeshCore sender integrity

The MeshCore sender-header normalization contract retains the original native
wire body as evidence but removes the claimed `name: ` header from canonical
text. Cross-transport tests verify that no header is relayed twice and the
claimed sender is visibly marked unverified. Projected sender fixtures use the
provider's native metadata schema version 2.
