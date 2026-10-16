# Tighten provider contracts and diagnostic lifecycle evidence

Run sessions now reject shutdown, snapshot, and required evidence failures in
every scenario, including deliberately injected faults. Accounting thresholds
count configured event/route/target deliveries. Normal smoke closes its runtime
after cancellation or unexpected routing/evidence errors. Cleanup exceptions
do not replace a structured failure report. Existing report fields carry these
failure reasons; the report schema shape is unchanged.

Fake smoke and run sessions reject enabled real adapters before runtime
construction, keeping their evidence classification and offline behavior honest.

The registration reference documents every `AdapterSpec` field, loading phase,
hook signature, fake construction, and explicit-keyword event-factory contract.
`medre.adapters.metadata` provides the public SDK-free metadata seam, and
`medre.testing` supplies optional fake and SDK pin conformance tools.

Registration value types now live in the discovery-free `medre.adapter_spec`
module. Importing a provider factory directly cannot start discovery before
that factory has been defined and accidentally quarantine its own provider.

Adapter-free ownership checks now include suite fixtures, optional imports,
namespace attributes, and literal `getattr` dependencies. Provider probes report
unavailable SDKs without allowing their exceptions to crash generic status CLI.

Native schemas/examples and their version-reader checks now belong to each
adapter repository, rather than duplicated core documents. Every repository
declares and enforces Python lint/format checks in CI. SDK installation keeps
the selected core checkout while installing provider-owned dependencies.
Installed-wheel sample proofs accept any sample-capable provider. Stale native
extra installation instructions are corrected.

The curated Docker bridge artifact harness moves from the installed runtime to
repository-only `scripts/ci/docker_bridge_artifacts.py`. Its shell entry point
and integration behavior are preserved; generic runtime packages no longer
carry first-party Docker scenario policy.

Runtime reporting delegates failure-detail and retryability classification to
the shared pure evidence implementation. Queue detail labels are now
`queue_rejected`, `outbound_suppressed`, and `queue_drain_cancelled`; no aliases
for transport-named detail labels are retained before the first release. Crypto
classification uses generic evidence such as E2EE/decryption errors, without
inferring ownership from a provider's SDK vocabulary.
