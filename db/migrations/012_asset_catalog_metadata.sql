-- Migration 012: Enrich asset catalog with Folloze content-report metadata
-- 2026-04-02

ALTER TABLE asset_catalog
    ADD COLUMN IF NOT EXISTS content_center_item_id TEXT,
    ADD COLUMN IF NOT EXISTS board_id TEXT,
    ADD COLUMN IF NOT EXISTS title_aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS source_url TEXT,
    ADD COLUMN IF NOT EXISTS item_source_value TEXT,
    ADD COLUMN IF NOT EXISTS item_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS category_1 TEXT;

CREATE INDEX IF NOT EXISTS idx_asset_catalog_board_id
    ON asset_catalog (board_id)
    WHERE board_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_asset_catalog_content_center_item_id
    ON asset_catalog (content_center_item_id)
    WHERE content_center_item_id IS NOT NULL;
