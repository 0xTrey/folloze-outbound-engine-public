-- Migration: 006_two_email_sequence
-- Cuts the sequence from 5 touches to 2.
-- Drops T3-T5 payload columns and their case study anchor columns.
-- T1+T2 remain unchanged.

ALTER TABLE personalization_payloads
  DROP COLUMN IF EXISTS ai_t3_body,
  DROP COLUMN IF EXISTS ai_t4_subject,
  DROP COLUMN IF EXISTS ai_t4_body,
  DROP COLUMN IF EXISTS ai_t5_subject,
  DROP COLUMN IF EXISTS ai_t5_body,
  DROP COLUMN IF EXISTS ai_t3_case_study_anchor,
  DROP COLUMN IF EXISTS ai_t4_case_study_anchor,
  DROP COLUMN IF EXISTS ai_t5_case_study_anchor;
