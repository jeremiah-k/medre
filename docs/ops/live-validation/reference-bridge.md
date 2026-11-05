# First live reference bridge: Matrix ↔ XMPP

This opt-in journey uses the existing disposable Docker Synapse, Matrix
Authentication Service, and Prosody lab with two *independent* peer accounts.
It drives real inbound messages through the MEDRE runtime in both directions,
then checks both recipients' native observations against the persisted SQLite
ledger. No fake-send, SDK-only, or unexecuted stage is called a success.

## Prerequisites

Use Linux x86-64 with Docker Compose and the core development environment.
Prepare a private profile containing explicit paths to **all seven** candidate
adapters and a candidate MMRelay checkout, as required by the existing lab
build machinery. These extra source checkouts are a **build provenance
requirement**; this reference campaign starts only the Matrix and XMPP
providers. Follow the [ecosystem-lab setup](docker-ecosystem.md) to create
a fresh, owner-only lab directory and populate its private development profile.

From the core checkout:

```console
lab_dir=/private/bench/reference-001
python -m scripts.adapter_campaign.lab prepare \
  --directory "$lab_dir" \
  --profile /private/bench/dev.yaml \
  --mmrelay /checkouts/mmrelay-candidate
python -m scripts.adapter_campaign.lab build --directory "$lab_dir"
python -m scripts.adapter_campaign.lab up --directory "$lab_dir"
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage reference-oauth
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage reference-provision
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage reference
python -m scripts.adapter_campaign.lab down --directory "$lab_dir"
```

Run `down` after failure too. Each test stage is independently bounded and
fails closed. `reference-oauth` qualifies the MEDRE Matrix OAuth device;
`reference-provision` creates only the Matrix room and XMPP MUC with their
independent peers. Neither requires MMRelay OAuth or provisions Chatmail,
LXMF, SimpleX, or radio accounts. The reference stage is opt-in and is *not*
a duplicate entry in the grouped native suite.

## Evidence and failure handling

After the `reference` stage succeeds, inspect the private directory
`"$lab_dir"/results/reference-*/`. The content-free `reference-verdict.json`
exists only after an independently verified native campaign and includes pathless
MEDRE/Matrix/XMPP source commits, snapshot digests, and dirty flags; the adjoining
`analysis.json` and `results.json` record the complete checked cases. The
private `matrix.jsonl`, `xmpp.jsonl`, `runtime-0.jsonl`, `runtime-1.jsonl`,
and `medre.sqlite3` contain underlying observations. Source checkouts and
image revisions are recorded in the lab's `sources.json` and `images.json`.

The ten required cases cover parent messages, replies from both sides, cross-
platform replies, a process restart preserving parent references, and
unavailable-parent fallback. The offline analyzer checks the source and
recipient peers, persisted canonical events, durable native references,
restart order, and duplicate/refusal expectations. **Do not edit the fixture
expectations to match a failing observation.** Preserve raw logs privately
and investigate the failed assertion.

This proves two-way **plaintext** communication only within the disposable
Synapse/Prosody environment. It does not prove Matrix encrypted peer trust,
production connectivity, hardware RF, all styles or attachments, or a right
to resend after uncertain native handoff. For deeper reply and semantic cases,
use [conversation fidelity](conversation-fidelity.md).
