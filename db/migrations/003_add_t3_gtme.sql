-- Migration 003: Add T3 touch field + register GTME_VIBE_MKT sequence family
-- 2026-03-12

-- Add T3 body column for 3-touch sequences (GTME_VIBE_MKT and future families)
ALTER TABLE personalization_payloads
    ADD COLUMN IF NOT EXISTS ai_t3_body TEXT;

-- Add subject and T2 columns if missing (schema.sql predates migrations 002/003)
ALTER TABLE personalization_payloads
    ADD COLUMN IF NOT EXISTS ai_subject TEXT,
    ADD COLUMN IF NOT EXISTS ai_t2_body TEXT,
    ADD COLUMN IF NOT EXISTS ai_t1_case_study_anchor TEXT,
    ADD COLUMN IF NOT EXISTS ai_t2_case_study_anchor TEXT;

-- Register the GTM Engineer sequence family
INSERT INTO sequence_families (family_name, segment, persona_type, trigger_type, is_active)
VALUES ('GTME_VIBE_MKT', 'GTME', 'gtm_engineer', NULL, TRUE)
ON CONFLICT (family_name) DO NOTHING;
