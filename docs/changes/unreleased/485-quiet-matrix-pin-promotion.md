# Promote quiet default Matrix encrypted delivery

Promote the merged Matrix provider revision that bypasses MEDRE's peer-device
trust preflight for default encrypted sends. Native Megolm encryption and key
sharing continue unchanged; opt-in strict authorization retains its local nio
trust requirements and send-time SDK enforcement.

Remove the `peer_device_trust_warnings` diagnostic rather than publishing a
misleading zero counter. Update dependent monitoring queries. Local Docker
qualification covers encrypted messages and files in both directions, graceful
restart, own-device signing, strict refusal, and independently decrypted delivery;
it establishes no federation or interactive peer-verification workflow.
