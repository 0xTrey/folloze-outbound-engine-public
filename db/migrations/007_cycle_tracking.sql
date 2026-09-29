-- Migration: 007_cycle_tracking
-- Adds 45-day re-engagement cycle tracking.
-- cycle_number on assignments tracks which re-engagement cycle this is (1 = first send).
-- last_sent_at on contacts is stamped by assign_push and used by re_engage to find
-- contacts who have been silent for >= 45 days.

ALTER TABLE campaign_assignments
  ADD COLUMN IF NOT EXISTS cycle_number INTEGER DEFAULT 1;

ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS last_sent_at TIMESTAMPTZ;
