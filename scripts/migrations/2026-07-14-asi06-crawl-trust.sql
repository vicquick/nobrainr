-- C2 ASI06 (2026-07-14): crawled external content is the untrusted-input
-- vector for memory poisoning. Lower its source-tier trust weight below
-- chatgpt-tier so poisoned crawl content ranks lower and fails a
-- trust_floor. Pairs with the crawl-path instruction sanitizer + the
-- serving trust_floor param.
CREATE OR REPLACE FUNCTION source_tier_weight(stype text) RETURNS real LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE stype
        WHEN 'manual' THEN 1.00 WHEN 'documentation' THEN 0.95
        WHEN 'session' THEN 0.85 WHEN 'agent' THEN 0.80
        WHEN 'agent_learning' THEN 0.75 WHEN 'cross_machine_insight' THEN 0.70
        WHEN 'claude' THEN 0.65 WHEN 'chatgpt' THEN 0.45
        WHEN 'crawl' THEN 0.40
        ELSE 0.50 END::real
$$;
