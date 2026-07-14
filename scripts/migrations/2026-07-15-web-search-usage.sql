-- Brave web_search monthly usage counter (R phase, 2026-07-15).
-- The Brave dashboard is capped at the free tier ($5 credits/mo ≈ 1000
-- queries) — this table gives (a) usage visibility for the metrics
-- pulse and (b) a clean early "quota exhausted, fall back to WebSearch"
-- error instead of a confusing Brave 401/429 when credits run dry.
CREATE TABLE IF NOT EXISTS web_search_usage (
    month   text PRIMARY KEY,   -- 'YYYY-MM'
    queries integer NOT NULL DEFAULT 0
);
