-- Migration 010: contact state snapshots
-- Records durable lifecycle snapshots for every outbound contact so we can
-- answer "what step are they in and what are they doing right now?" without
-- rebuilding state from Smartlead and ad hoc SQL every time.

CREATE TABLE IF NOT EXISTS contact_state_snapshots (
    snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
    assignment_id UUID REFERENCES campaign_assignments(assignment_id) ON DELETE SET NULL,
    email TEXT,
    company_name TEXT,
    sequence_family TEXT,
    eligibility_status TEXT,
    payload_status TEXT,
    research_status TEXT,
    smartlead_campaign_id TEXT,
    smartlead_campaign_status TEXT,
    smartlead_campaign_lead_map_id TEXT,
    smartlead_prospect_id TEXT,
    smartlead_lead_status TEXT,
    current_step_number INTEGER,
    current_step_subject TEXT,
    delivery_status TEXT,
    reply_type TEXT,
    current_activity TEXT NOT NULL,
    activity_detail TEXT,
    last_sent_at TIMESTAMPTZ,
    last_opened_at TIMESTAMPTZ,
    last_clicked_at TIMESTAMPTZ,
    last_replied_at TIMESTAMPTZ,
    last_activity_at TIMESTAMPTZ,
    raw_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contact_state_snapshots_contact_created
    ON contact_state_snapshots (contact_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_contact_state_snapshots_activity
    ON contact_state_snapshots (current_activity, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_contact_state_snapshots_assignment_created
    ON contact_state_snapshots (assignment_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_contact_state_snapshots_smartlead_ids
    ON contact_state_snapshots (smartlead_campaign_id, smartlead_campaign_lead_map_id);

CREATE OR REPLACE VIEW contact_state_current AS
SELECT DISTINCT ON (contact_id)
    snapshot_id,
    contact_id,
    assignment_id,
    email,
    company_name,
    sequence_family,
    eligibility_status,
    payload_status,
    research_status,
    smartlead_campaign_id,
    smartlead_campaign_status,
    smartlead_campaign_lead_map_id,
    smartlead_prospect_id,
    smartlead_lead_status,
    current_step_number,
    current_step_subject,
    delivery_status,
    reply_type,
    current_activity,
    activity_detail,
    last_sent_at,
    last_opened_at,
    last_clicked_at,
    last_replied_at,
    last_activity_at,
    raw_snapshot,
    created_at
FROM contact_state_snapshots
ORDER BY contact_id, created_at DESC;
