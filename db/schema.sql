-- accounts
CREATE TABLE accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain TEXT NOT NULL UNIQUE,
    company_name TEXT,
    industry TEXT,
    employee_count INTEGER,
    revenue_band TEXT,
    geography TEXT,
    fit_score NUMERIC(4,2),
    timing_score NUMERIC(4,2),
    priority_score NUMERIC(4,2),
    segment TEXT,               -- ENT, MM, SMB
    owner_rep TEXT,
    crm_account_id TEXT,
    account_status TEXT DEFAULT 'active',  -- active | customer | open_opp | suppressed
    apollo_raw JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- contacts
CREATE TABLE contacts (
    contact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(account_id),
    first_name TEXT,
    last_name TEXT,
    title TEXT,
    seniority TEXT,
    department TEXT,
    email TEXT,
    email_status TEXT,          -- verified | risky | invalid | unknown
    linkedin_url TEXT,
    persona_type TEXT,          -- revmktg_vp | revops | abm_leader | cmo | practitioner
    source TEXT DEFAULT 'apollo',
    last_enriched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- campaign_assignments
CREATE TABLE campaign_assignments (
    assignment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(contact_id),
    sequence_family TEXT,
    trigger_type TEXT,
    campaign_type TEXT,
    offer_type TEXT,
    mailbox_pool TEXT,
    mailbox_id UUID,
    scheduled_at TIMESTAMPTZ,
    eligibility_status TEXT DEFAULT 'pending',  -- pending | eligible | ineligible | sent
    ineligibility_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- personalization_payloads
CREATE TABLE personalization_payloads (
    payload_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(contact_id),
    template_version TEXT,
    deterministic_tokens JSONB,
    ai_opener TEXT,
    ai_why_now TEXT,
    ai_proof_point TEXT,
    ai_cta TEXT,
    model_used TEXT,
    prompt_version TEXT,
    qa_status TEXT DEFAULT 'pending',  -- pending | approved | failed
    qa_failure_reason TEXT,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- sends
CREATE TABLE sends (
    send_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(contact_id),
    smartlead_campaign_id TEXT,
    smartlead_lead_id TEXT,
    mailbox TEXT,
    domain TEXT,
    step_number INTEGER,
    sent_at TIMESTAMPTZ,
    delivery_status TEXT,
    bounce_type TEXT,
    reply_type TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- suppressions
CREATE TABLE suppressions (
    suppression_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT,
    domain TEXT,
    reason TEXT,   -- bounce | unsubscribe | customer | open_opp | manual
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

-- mailboxes
CREATE TABLE mailboxes (
    mailbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_address TEXT NOT NULL UNIQUE,
    sending_domain TEXT,
    provider TEXT,
    warmup_status TEXT DEFAULT 'warming',   -- warming | active | paused | dead
    health_status TEXT DEFAULT 'healthy',
    daily_cap INTEGER DEFAULT 30,
    current_daily_sent INTEGER DEFAULT 0,
    current_bounce_rate NUMERIC(4,2),
    current_reply_rate NUMERIC(4,2),
    last_checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- contact_research
-- Stores the full research brief per contact. Populated by enrich_research,
-- read by build_payloads. One row per contact (upserted on re-run).
CREATE TABLE contact_research (
    research_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id          UUID NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
    brief_text          TEXT,        -- pre-formatted string for prompt injection
    brief_summary       TEXT,        -- LLM-generated 2-3 sentence human-readable summary
    hook_angle          TEXT,
    company_snapshot    TEXT,
    people_intel        TEXT,
    trigger_events      JSONB,
    tech_context        TEXT,
    competitive_notes   TEXT,
    recent_posts        JSONB,
    sources_used        TEXT[],
    model_used          TEXT,
    enriched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT contact_research_contact_unique UNIQUE (contact_id)
);

-- sequence_families
CREATE TABLE sequence_families (
    family_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_name TEXT NOT NULL UNIQUE,   -- ENT_REVMKTG_INTENT, MM_CMO_EXEC, etc.
    segment TEXT,
    persona_type TEXT,
    trigger_type TEXT,
    offer_type TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- contact_state_snapshots
-- Durable lifecycle snapshots for export, migration planning, and current-state reporting.
CREATE TABLE contact_state_snapshots (
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

-- asset_catalog
-- Semantic catalog for Folloze content items and destinations referenced in follow-up copy.
CREATE TABLE asset_catalog (
    asset_id TEXT PRIMARY KEY,
    content_center_item_id TEXT,
    board_id TEXT,
    title TEXT,
    title_aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_type TEXT,
    summary TEXT,
    themes JSONB NOT NULL DEFAULT '[]'::jsonb,
    followup_angle TEXT,
    cta_type TEXT,
    destination_board TEXT,
    status TEXT DEFAULT 'active',
    source TEXT,
    source_url TEXT,
    item_source_value TEXT,
    item_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    category_1 TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- signal_events
-- Immutable-ish raw event ledger. Each source event is upserted by source + external_event_id.
CREATE TABLE signal_events (
    signal_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_timestamp_utc TIMESTAMPTZ NOT NULL,
    ingested_timestamp_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    contact_id UUID REFERENCES contacts(contact_id) ON DELETE SET NULL,
    account_id UUID REFERENCES accounts(account_id) ON DELETE SET NULL,
    crm_person_id TEXT,
    crm_person_type TEXT,
    content_id TEXT,
    board_id TEXT,
    campaign_id TEXT,
    touch_number INTEGER,
    duration_seconds INTEGER,
    url TEXT,
    confidence NUMERIC(5,4) DEFAULT 1.0,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT signal_events_source_external_unique UNIQUE (source, external_event_id)
);

-- fact_graph_events
-- Evidence-backed facts captured from research and public sources.
CREATE TABLE fact_graph_events (
    fact_graph_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    source_url TEXT,
    source_name TEXT,
    observed_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confidence NUMERIC(5,4),
    expiry_at TIMESTAMPTZ,
    is_foundational_firmographic BOOLEAN NOT NULL DEFAULT FALSE,
    requires_two_source_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_at TIMESTAMPTZ,
    confirmed_by_fact_id UUID REFERENCES fact_graph_events(fact_graph_event_id) ON DELETE SET NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- messaging_primitives
-- Prompt-safe abstractions derived from evidence and behavior.
CREATE TABLE messaging_primitives (
    primitive_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(contact_id) ON DELETE CASCADE,
    account_id UUID REFERENCES accounts(account_id) ON DELETE CASCADE,
    primitive_type TEXT NOT NULL,
    text TEXT NOT NULL,
    source_fact_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    confidence NUMERIC(5,4),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- account_signal_state
-- Rolling interest memory for an account.
CREATE TABLE account_signal_state (
    account_signal_state_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL UNIQUE REFERENCES accounts(account_id) ON DELETE CASCADE,
    topic_distribution_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    buying_group_coverage_score NUMERIC(6,3),
    intent_strength NUMERIC(6,3),
    competitive_context TEXT,
    open_opp_flag BOOLEAN NOT NULL DEFAULT FALSE,
    last_meaningful_engagement_at TIMESTAMPTZ,
    recommended_play_id TEXT,
    state_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- contact_signal_state
-- Prompt-ready interest memory for a person.
CREATE TABLE contact_signal_state (
    contact_signal_state_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL UNIQUE REFERENCES contacts(contact_id) ON DELETE CASCADE,
    account_id UUID REFERENCES accounts(account_id) ON DELETE SET NULL,
    last_activity_at TIMESTAMPTZ,
    engagement_level TEXT,
    top_theme TEXT,
    top_asset_id TEXT,
    top_asset_summary TEXT,
    repeat_visitor_flag BOOLEAN NOT NULL DEFAULT FALSE,
    cta_click_flag BOOLEAN NOT NULL DEFAULT FALSE,
    single_asset_deep_view_flag BOOLEAN NOT NULL DEFAULT FALSE,
    multi_asset_same_theme_flag BOOLEAN NOT NULL DEFAULT FALSE,
    days_visited INTEGER,
    number_of_items_viewed INTEGER,
    total_view_time_seconds INTEGER,
    signal_confidence NUMERIC(6,3),
    recommended_next_angle TEXT,
    recommended_destination_board TEXT,
    recommended_play_id TEXT,
    state_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- outbound_briefs
-- Structured brief contract passed to the generation layer.
CREATE TABLE outbound_briefs (
    brief_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(account_id) ON DELETE SET NULL,
    contact_id UUID REFERENCES contacts(contact_id) ON DELETE SET NULL,
    source_state_updated_at TIMESTAMPTZ,
    brief_fingerprint TEXT,
    why_now JSONB NOT NULL DEFAULT '[]'::jsonb,
    why_them TEXT,
    topic_focus JSONB NOT NULL DEFAULT '[]'::jsonb,
    proof_points_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_recommendation_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    asset_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    primitive_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    fact_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    evidence_points INTEGER,
    gate_status TEXT,
    gate_reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    cta TEXT,
    forbidden_claims_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    tone_profile TEXT,
    play_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- touch_payload_versions
-- Stores exactly what signal state and brief produced a generated follow-up touch.
CREATE TABLE touch_payload_versions (
    payload_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
    touch_number INTEGER NOT NULL,
    brief_id UUID REFERENCES outbound_briefs(brief_id) ON DELETE SET NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signal_state_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    asset_ids_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    primitive_ids_used UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    model_profiles_used TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    prompt_version TEXT,
    payload_body TEXT,
    send_status TEXT,
    outcome_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signal_events_source_ingested
    ON signal_events (source, ingested_timestamp_utc DESC);

CREATE INDEX idx_contact_signal_state_updated_at
    ON contact_signal_state (updated_at DESC);

CREATE INDEX idx_outbound_briefs_contact_created
    ON outbound_briefs (contact_id, created_at DESC)
    WHERE contact_id IS NOT NULL;

CREATE INDEX idx_outbound_briefs_gate_status
    ON outbound_briefs (gate_status)
    WHERE gate_status IS NOT NULL;

CREATE UNIQUE INDEX idx_outbound_briefs_contact_fingerprint_unique
    ON outbound_briefs (contact_id, brief_fingerprint)
    WHERE contact_id IS NOT NULL
      AND brief_fingerprint IS NOT NULL;
