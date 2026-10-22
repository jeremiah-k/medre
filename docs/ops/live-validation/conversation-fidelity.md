# Native conversation fidelity campaigns

Use this campaign to qualify Matrix/XMPP reply targets, attribution, readable
fallback, Unicode text, and stored-parent correlation across an actual runtime
restart. Native peers use independent accounts and SDK/client-protocol paths;
the harness does not inject canonical events or edit stored references.

## Docker setup

Prepare and start the [ecosystem lab](docker-ecosystem.md), then run:

```console
lab_dir=/private/bench/campaign-001
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage oauth
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage provision
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage fidelity
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage logs
python -m scripts.adapter_campaign.lab down --directory "$lab_dir"
```

The grouped `native` and `all` stages include `fidelity`. Run `down` after a
failure too. This tier uses no radios or host networking changes. Keep candidate
provider selections in a private local development profile; committed
compatibility pins identify merged revisions.

## Required cases

The campaign sends a parent from each platform and exercises replies from both
the originating platform and the bridged conversation. Independent recipients
must observe the correct native parent ID in their own conversation. XMPP room
references use room-issued IDs; client-generated stanza IDs remain separate.
Source acknowledgements use preserved origin IDs even if the room rewrites the
outer stanza ID.

The runtime then stops and starts against the same SQLite database. Restart
has its own bounded deadline (the configured case timeout plus 30 seconds for
shutdown and cleanup); it does not consume the following native reply's case
timeout. Replies to both stored parents must bind correctly after restart, with
unchanged parent events and delivery evidence. A final pair of replies names
unavailable native targets and must preserve the answer and readable reply
context without sending an unrelated native relation.

Native quote ranges use Unicode code points. Quoted parent text containing an
earlier nonce is not counted as another delivery of that parent. The oracle
also checks configured sender/conversation/prefix expectations, independent
source acknowledgements, Matrix HTML/plain-text parity, canonical bodies and
relations, committed receipt pointers, exact outbound native references,
duplicate counts, and cleanup.

Native reply support does not imply threaded-conversation support. The campaign
does not qualify edits, reactions, rich styling, ephemeral typing, attachments,
encrypted-room traffic, or arbitrary message-history recovery. Cross-room and
malformed-reference refusals also have deterministic adapter regressions.

## Private fixture and retained evidence

Use the existing [campaign fixture](adapter-campaign.md), selecting its Matrix
and XMPP entries. The bot and peer must belong to the same routed conversation
and use different authenticated identities. Preview without sending:

```console
python -m scripts.adapter_campaign.fidelity --config /private/bench/campaign.yaml
```

Permit native sends and an owned runtime restart:

```console
python -m scripts.adapter_campaign.fidelity \
  --config /private/bench/campaign.yaml \
  --output /private/bench/fidelity-001 --run
python -m scripts.analyze_fidelity_campaign /private/bench/fidelity-001
```

The output directory must be fresh. It retains peer streams, two runtime
generations, one SQLite database, fixture-authored expectations, and case
snapshots. Raw captures contain private conversation data and credentials;
commit the harness and sanitized fixtures rather than generated evidence.

Offline analysis reopens SQLite read-only and rechecks every case against the
native streams. It requires both stdout and stderr captures, rejects resource
warnings and incomplete coverage, and verifies that restart cases occurred
after the second generation became ready. Its public failure summary includes
an error type without raw exception or message text.

The opt-in pytest entry point is `tests/test_native_fidelity_campaign_live.py`.
Set `MEDRE_FIDELITY_CAMPAIGN` and optionally `MEDRE_FIDELITY_OUTPUT`, then select
that file with `-m live`. Ordinary CI runs the offline controls and oracles.
