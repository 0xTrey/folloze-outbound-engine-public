-- Migration: 004_case_study_anchors
-- Adds optional case study anchor text columns to personalization_payloads.
-- LLM writes short anchor text per touch (or "" if no link appropriate).
-- assign_push.py builds the full HTML <a href> link at push time using contact identity.

ALTER TABLE personalization_payloads
  ADD COLUMN IF NOT EXISTS ai_t1_case_study_anchor TEXT,
  ADD COLUMN IF NOT EXISTS ai_t2_case_study_anchor TEXT,
  ADD COLUMN IF NOT EXISTS ai_t3_case_study_anchor TEXT,
  ADD COLUMN IF NOT EXISTS ai_t4_case_study_anchor TEXT,
  ADD COLUMN IF NOT EXISTS ai_t5_case_study_anchor TEXT;
