-- Migration: 008_apollo_id
-- Stores Apollo person ID on contacts so we can bulk-reveal emails
-- via people/bulk_match after the initial api_search.

ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS apollo_id TEXT;
