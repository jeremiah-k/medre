-- Receipt-less handoff settlement: immutable, content-free append-order fence.
-- The receipt watermark is captured in the terminal transition transaction.
CREATE TABLE IF NOT EXISTS delivery_handoff_settlements (
    outbox_id TEXT PRIMARY KEY REFERENCES delivery_outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    receipt_watermark INTEGER NOT NULL CHECK (receipt_watermark >= 0)
);
