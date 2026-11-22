-- Content-free evidence identity retained through a hard ceiling. These
-- records do NOT contain radio payload, submission grants or free text.
-- Content-free exact-attempt uncertainty evidence. The free-form diagnostic
-- can be purged at the hard ceiling without losing late-ACK correlation.
-- An explicit non-uncertain failure classification durably transitions the
-- same attempt to ``superseded_native_handoff`` so a later purge of the
-- diagnostic cannot revive closed uncertainty. Neither state grants another
-- transport dispatch.
CREATE TABLE IF NOT EXISTS delivery_handoff_uncertainty (
    outbox_id TEXT NOT NULL PRIMARY KEY REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    state TEXT NOT NULL CHECK (state IN ('uncertain_native_handoff', 'superseded_native_handoff'))
);

-- Content-free attempt progress survives deletion of radio wire plans.
-- States include accepted, submitting and unknown; the projection is
-- evidence of progress, not by itself proof of acceptance.
CREATE TABLE IF NOT EXISTS radio_attempt_evidence_summaries (
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
    parts_json TEXT NOT NULL CHECK (json_valid(parts_json)),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (outbox_id, attempt_number)
);

-- Non-authorizing packet scope survives clearing observation metadata.
CREATE TABLE IF NOT EXISTS delivery_observation_scopes (
    observation_id TEXT NOT NULL PRIMARY KEY REFERENCES delivery_observations(observation_id),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json))
);

-- Acceptance facts survive deletion of radio wire plans at the ceiling.
-- Only a previously accepted, exact original packet is recorded. Retaining
-- the identity never reconstructs a payload or authorizes submission.
CREATE TABLE IF NOT EXISTS radio_accepted_part_facts (
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 128),
    native_channel_id TEXT,
    native_message_id TEXT NOT NULL CHECK (length(native_message_id) > 0),
    PRIMARY KEY (outbox_id, attempt_number, ordinal)
);
