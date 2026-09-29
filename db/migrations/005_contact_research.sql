-- Migration 005: contact_research table
-- Stores the full research brief for each contact, collected once and reused
-- across all payload generation runs. build_payloads reads from this table
-- instead of calling Perplexity / Brave / X live on every run.

CREATE TABLE IF NOT EXISTS contact_research (
    research_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id          UUID NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,

    -- Pre-formatted brief string injected directly into the build_payloads prompt.
    -- Output of research/prospect_researcher.format_brief_for_prompt().
    brief_text          TEXT,

    -- Human-readable 2-3 sentence summary for review / display. LLM-generated.
    brief_summary       TEXT,

    -- Structured fields kept separately for querying and future display use.
    hook_angle          TEXT,
    company_snapshot    TEXT,
    people_intel        TEXT,
    trigger_events      JSONB,   -- [{type: str, text: str}]
    tech_context        TEXT,
    competitive_notes   TEXT,
    recent_posts        JSONB,   -- [{text: str}]

    -- Metadata
    sources_used        TEXT[],  -- e.g. ["perplexity", "brave", "x"]
    model_used          TEXT,    -- model used for brief_summary generation
    enriched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT contact_research_contact_unique UNIQUE (contact_id)
);

CREATE INDEX IF NOT EXISTS idx_contact_research_contact_id  ON contact_research(contact_id);
CREATE INDEX IF NOT EXISTS idx_contact_research_enriched_at ON contact_research(enriched_at);
