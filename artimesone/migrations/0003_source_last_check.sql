-- Round-based scheduling: track when each source was last visited so the
-- round job can pick the 5 oldest (NULLs first) and apply a 24h cooldown
-- gate. See scheduler.run_collection_round.

ALTER TABLE sources ADD COLUMN last_check_at TEXT;

CREATE INDEX idx_sources_last_check ON sources(enabled, last_check_at);
