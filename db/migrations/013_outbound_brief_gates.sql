-- Migration 013: Add send-gate metadata and lineage to outbound briefs
-- 2026-04-03

ALTER TABLE outbound_briefs
    ADD COLUMN IF NOT EXISTS source_state_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS asset_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS primitive_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS fact_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS evidence_points INTEGER,
    ADD COLUMN IF NOT EXISTS gate_status TEXT,
    ADD COLUMN IF NOT EXISTS gate_reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_outbound_briefs_contact_created
    ON outbound_briefs (contact_id, created_at DESC)
    WHERE contact_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_outbound_briefs_gate_status
    ON outbound_briefs (gate_status)
    WHERE gate_status IS NOT NULL;
