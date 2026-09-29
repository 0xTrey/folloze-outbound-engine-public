-- Migration 011: Signal engine foundation tables
-- 2026-04-02

CREATE TABLE IF NOT EXISTS asset_catalog (
    asset_id TEXT PRIMARY KEY,
    title TEXT,
    content_type TEXT,
    summary TEXT,
    themes JSONB NOT NULL DEFAULT '[]'::jsonb,
    followup_angle TEXT,
    cta_type TEXT,
    destination_board TEXT,
    status TEXT DEFAULT 'active',
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS signal_events (
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

CREATE TABLE IF NOT EXISTS fact_graph_events (
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

CREATE TABLE IF NOT EXISTS messaging_primitives (
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

CREATE TABLE IF NOT EXISTS account_signal_state (
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

CREATE TABLE IF NOT EXISTS contact_signal_state (
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

CREATE TABLE IF NOT EXISTS outbound_briefs (
    brief_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(account_id) ON DELETE SET NULL,
    contact_id UUID REFERENCES contacts(contact_id) ON DELETE SET NULL,
    why_now JSONB NOT NULL DEFAULT '[]'::jsonb,
    why_them TEXT,
    topic_focus JSONB NOT NULL DEFAULT '[]'::jsonb,
    proof_points_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_recommendation_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    cta TEXT,
    forbidden_claims_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    tone_profile TEXT,
    play_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS touch_payload_versions (
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

CREATE INDEX IF NOT EXISTS idx_signal_events_event_time
    ON signal_events (event_timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_signal_events_source_event_type
    ON signal_events (source, event_type);

CREATE INDEX IF NOT EXISTS idx_signal_events_board_id
    ON signal_events (board_id)
    WHERE board_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signal_events_content_id
    ON signal_events (content_id)
    WHERE content_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fact_graph_entity
    ON fact_graph_events (entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_fact_graph_expiry
    ON fact_graph_events (expiry_at)
    WHERE expiry_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messaging_primitives_contact
    ON messaging_primitives (contact_id)
    WHERE contact_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messaging_primitives_account
    ON messaging_primitives (account_id)
    WHERE account_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_touch_payload_versions_contact_touch
    ON touch_payload_versions (contact_id, touch_number, generated_at DESC);
