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

-- An exact-attempt serial for every stored packet-scope insertion, repair, or
-- removal. A scope row count cannot fence an in-place repair: revisions are
-- advanced by triggers in the same transaction as every scope mutation.
-- Only content-free identities and a monotone integer are retained.
CREATE TABLE IF NOT EXISTS delivery_observation_scope_versions (
    outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    PRIMARY KEY (outbox_id, attempt_number)
);

CREATE TRIGGER IF NOT EXISTS trg_scope_version_insert
AFTER INSERT ON delivery_observation_scopes
BEGIN
    INSERT INTO delivery_observation_scope_versions (outbox_id, attempt_number, revision)
    SELECT o.outbox_id, o.attempt_number, 1 FROM delivery_observations o
    WHERE o.observation_id=NEW.observation_id
    ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER IF NOT EXISTS trg_scope_version_update
AFTER UPDATE OF scope_json ON delivery_observation_scopes
WHEN OLD.scope_json IS NOT NEW.scope_json
BEGIN
    INSERT INTO delivery_observation_scope_versions (outbox_id, attempt_number, revision)
    SELECT o.outbox_id, o.attempt_number, 1 FROM delivery_observations o
    WHERE o.observation_id=NEW.observation_id
    ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET revision=revision+1;
END;

CREATE TRIGGER IF NOT EXISTS trg_scope_version_delete
AFTER DELETE ON delivery_observation_scopes
BEGIN
    INSERT INTO delivery_observation_scope_versions (outbox_id, attempt_number, revision)
    SELECT o.outbox_id, o.attempt_number, 1 FROM delivery_observations o
    WHERE o.observation_id=OLD.observation_id
    ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET revision=revision+1;
END;

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
