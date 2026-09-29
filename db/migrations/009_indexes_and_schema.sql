-- Migration 009: Indexes, kv_config, sequence_family on sends, processing status
-- 2026-03-16

-- Track applied migrations (bootstrap table for migration runner)
CREATE TABLE IF NOT EXISTS applied_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- Lightweight key-value store for job state (poll cursors, etc.)
CREATE TABLE IF NOT EXISTS kv_config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Add sequence_family column to sends (fixes smartlead_campaign_id semantic mismatch)
ALTER TABLE sends
    ADD COLUMN IF NOT EXISTS sequence_family TEXT;

-- Add processing_at to campaign_assignments for atomic claim pattern
ALTER TABLE campaign_assignments
    ADD COLUMN IF NOT EXISTS processing_at TIMESTAMPTZ;

-- Indexes on high-frequency filter columns
CREATE INDEX IF NOT EXISTS idx_campaign_assignments_eligibility
    ON campaign_assignments (eligibility_status);

CREATE INDEX IF NOT EXISTS idx_sends_sent_at
    ON sends (sent_at);

CREATE INDEX IF NOT EXISTS idx_suppressions_email
    ON suppressions (email) WHERE email IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_suppressions_domain
    ON suppressions (domain) WHERE domain IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_payloads_qa_status
    ON personalization_payloads (qa_status);

CREATE INDEX IF NOT EXISTS idx_payloads_contact_id
    ON personalization_payloads (contact_id);
