-- Add view_count and like_count as first-class columns on items.
-- Captured once at discovery time from YouTube Data API statistics;
-- not refreshed, not tracked over time. Both nullable: creators can
-- hide likes, and existing rows stay NULL (no backfill).

ALTER TABLE items ADD COLUMN view_count INTEGER;
ALTER TABLE items ADD COLUMN like_count INTEGER;
