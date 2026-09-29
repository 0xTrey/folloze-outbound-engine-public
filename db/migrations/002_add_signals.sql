-- Migration 002: Add signals column to accounts
-- Stores Perplexity-enriched company intelligence (news, hiring, funding, etc.)

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS signals JSONB,
    ADD COLUMN IF NOT EXISTS signals_enriched_at TIMESTAMPTZ;

-- Index for fast lookups of un-enriched or stale accounts
CREATE INDEX IF NOT EXISTS idx_accounts_signals_enriched_at
    ON accounts (signals_enriched_at NULLS FIRST);
