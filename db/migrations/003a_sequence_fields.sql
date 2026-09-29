-- Migration: 003_sequence_fields
-- Adds 7 new personalization payload columns for the 5-touch sequence
-- and a smartlead_campaign_id FK column on sequence_families.
-- Idempotent: all ADD COLUMN IF NOT EXISTS.

ALTER TABLE personalization_payloads
  ADD COLUMN IF NOT EXISTS ai_subject      TEXT,
  ADD COLUMN IF NOT EXISTS ai_t2_body      TEXT,
  ADD COLUMN IF NOT EXISTS ai_t3_body      TEXT,
  ADD COLUMN IF NOT EXISTS ai_t4_subject   TEXT,
  ADD COLUMN IF NOT EXISTS ai_t4_body      TEXT,
  ADD COLUMN IF NOT EXISTS ai_t5_subject   TEXT,
  ADD COLUMN IF NOT EXISTS ai_t5_body      TEXT;

ALTER TABLE sequence_families
  ADD COLUMN IF NOT EXISTS smartlead_campaign_id TEXT;
