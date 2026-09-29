-- Migration 014: Brief fingerprint hardening and canary-supporting indexes
-- 2026-04-11

ALTER TABLE outbound_briefs
    ADD COLUMN IF NOT EXISTS brief_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_signal_events_source_ingested
    ON signal_events (source, ingested_timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_contact_signal_state_updated_at
    ON contact_signal_state (updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_briefs_contact_fingerprint_unique
    ON outbound_briefs (contact_id, brief_fingerprint)
    WHERE contact_id IS NOT NULL
      AND brief_fingerprint IS NOT NULL;
