-- Trust layer migration. Additive only. Run inside the nobrainr DB.
--
-- Adds verification + claim-kind + supersede chain to memories so the
-- retriever can distinguish "true now" from "was true once". Pairs with
-- /opt/nobrainr/scripts/migrations/2026-04-27-trust-backfill.py for the
-- heuristic claim_kind backfill, and with /usr/local/bin/nobrainr-verify
-- for the hourly probe runner.
--
-- Idempotent: every ADD COLUMN uses IF NOT EXISTS, every CREATE uses
-- IF NOT EXISTS or CREATE OR REPLACE.

BEGIN;

-- 1. Trust columns on memories
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS verified_at          timestamptz,
    ADD COLUMN IF NOT EXISTS verified_by          text,
    ADD COLUMN IF NOT EXISTS verification_method  text,
    ADD COLUMN IF NOT EXISTS claim_kind           text,
    ADD COLUMN IF NOT EXISTS max_staleness_days   integer,
    ADD COLUMN IF NOT EXISTS applicable_to        text[] DEFAULT ARRAY[]::text[],
    ADD COLUMN IF NOT EXISTS supersedes           uuid REFERENCES memories(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS superseded_by        uuid REFERENCES memories(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS contradicted_by      uuid REFERENCES memories(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS trust_score          real;

-- 2. Constraints
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_claim_kind_chk;
ALTER TABLE memories ADD  CONSTRAINT memories_claim_kind_chk
    CHECK (claim_kind IS NULL OR claim_kind IN (
        'code-state',      -- which model is loaded, what file is running, runtime config
        'infra-state',     -- deployed apps, ports, container names, network topology
        'preference',      -- user/agent preferences, communication style
        'incident-fix',    -- root cause + fix, append-only, never decays
        'design-decision', -- why we chose X over Y, decays slowly
        'historical',      -- what was true on date X, immutable snapshot
        'reference',       -- pointer to external system (URL, dashboard, repo)
        'fact'             -- domain fact (CPU model, IP address, version number)
    ));

-- 3. Indexes for trust ranking + staleness scan
CREATE INDEX IF NOT EXISTS idx_memories_claim_kind         ON memories (claim_kind) WHERE claim_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_verified_at        ON memories (verified_at);
CREATE INDEX IF NOT EXISTS idx_memories_superseded_by      ON memories (superseded_by) WHERE superseded_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_contradicted_by    ON memories (contradicted_by) WHERE contradicted_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_trust_score        ON memories (trust_score DESC NULLS LAST);

-- Partial: rows that need the staleness scanner
CREATE INDEX IF NOT EXISTS idx_memories_stale_candidates ON memories (claim_kind, verified_at)
    WHERE claim_kind IN ('code-state','infra-state') AND superseded_by IS NULL;

-- 4. Verification probe registry
CREATE TABLE IF NOT EXISTS verification_probes (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),
    probe_name      text NOT NULL UNIQUE,
    claim_pattern   text NOT NULL,                       -- regex matched against content||' '||summary
    tag_filter      text[] DEFAULT ARRAY[]::text[],      -- ANY-of, optional narrowing
    probe_type      text NOT NULL,                       -- 'shell' | 'http' | 'sql' | 'file'
    probe_command   text NOT NULL,                       -- shell: cmd; http: url; sql: query; file: path
    expected_regex  text,                                -- if set, probe output must match
    claim_kind      text NOT NULL,                       -- which kind this probe verifies
    max_staleness_days integer NOT NULL DEFAULT 7,
    enabled         boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_run_at     timestamptz,
    last_run_status text,
    notes           text
);

-- 5. Verification audit log
CREATE TABLE IF NOT EXISTS verification_log (
    id           uuid PRIMARY KEY DEFAULT uuidv7(),
    memory_id    uuid REFERENCES memories(id) ON DELETE CASCADE,
    probe_id     uuid REFERENCES verification_probes(id) ON DELETE SET NULL,
    ran_at       timestamptz NOT NULL DEFAULT now(),
    result       text NOT NULL,                          -- 'verified' | 'stale' | 'mismatch' | 'probe-error'
    probe_output text,
    notes        text
);
CREATE INDEX IF NOT EXISTS idx_verification_log_memory ON verification_log (memory_id, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_log_result ON verification_log (result, ran_at DESC);

-- 6. Default max_staleness_days per claim_kind (used when memory's own value is NULL)
CREATE OR REPLACE FUNCTION default_max_staleness_days(kind text)
RETURNS integer
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE kind
        WHEN 'code-state'      THEN 1
        WHEN 'infra-state'     THEN 7
        WHEN 'preference'      THEN 90
        WHEN 'design-decision' THEN 365
        WHEN 'reference'       THEN 30
        WHEN 'fact'            THEN 90
        WHEN 'incident-fix'    THEN NULL          -- never decays
        WHEN 'historical'      THEN NULL          -- immutable
        ELSE 30
    END
$$;

-- 7. Source tier weight (manual ground truth > agent > crawl > distilled)
CREATE OR REPLACE FUNCTION source_tier_weight(stype text)
RETURNS real
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE stype
        WHEN 'manual'         THEN 1.00
        WHEN 'documentation'  THEN 0.95
        WHEN 'session'        THEN 0.85
        WHEN 'agent'          THEN 0.80
        WHEN 'agent_learning' THEN 0.75
        WHEN 'cross_machine_insight' THEN 0.70
        WHEN 'claude'         THEN 0.65
        WHEN 'crawl'          THEN 0.55
        WHEN 'chatgpt'        THEN 0.45
        ELSE 0.50
    END::real
$$;

-- 8. Verification freshness score (0..1)
--    1.0 if verified within staleness window
--    Linear decay to 0 over 2x window
--    NULL verified_at: 0.3 (unknown but not actively wrong)
CREATE OR REPLACE FUNCTION verification_freshness(
    verified_at timestamptz,
    kind        text,
    override_days integer
) RETURNS real
LANGUAGE sql
IMMUTABLE
AS $$
    WITH params AS (
        SELECT COALESCE(override_days, default_max_staleness_days(kind)) AS days
    )
    SELECT CASE
        WHEN kind IN ('incident-fix','historical') THEN 1.0::real
        WHEN verified_at IS NULL THEN 0.30::real
        WHEN (SELECT days FROM params) IS NULL THEN 1.0::real
        ELSE GREATEST(
            0.0,
            LEAST(
                1.0,
                1.0 - (
                    EXTRACT(EPOCH FROM (now() - verified_at))
                    / (86400.0 * 2.0 * (SELECT days FROM params))
                )
            )
        )::real
    END
$$;

-- 9. Trust score: deterministic, no LLM
--    0.40 verification freshness
--    0.25 contradiction safety (1 if not contradicted by fresher, 0 if it is)
--    0.20 outcome score (rolling avg of memory_outcomes.was_useful, last 30d, default 0.5)
--    0.10 source tier
--    0.05 stability
CREATE OR REPLACE FUNCTION compute_trust_score(memory_id uuid)
RETURNS real
LANGUAGE sql
STABLE
AS $$
    WITH m AS (
        SELECT m.id, m.verified_at, m.claim_kind, m.max_staleness_days,
               m.contradicted_by, m.source_type, m.stability, m.superseded_by
          FROM memories m WHERE m.id = memory_id
    ),
    contradiction AS (
        SELECT CASE
            WHEN (SELECT contradicted_by FROM m) IS NULL THEN 1.0::real
            WHEN (SELECT superseded_by  FROM m) IS NOT NULL THEN 0.0::real
            ELSE 0.3::real
        END AS safety
    ),
    outcome AS (
        SELECT COALESCE(
            AVG(CASE WHEN was_useful THEN 1.0 ELSE 0.0 END)::real,
            0.5::real
        ) AS score
        FROM memory_outcomes
        WHERE memory_id = (SELECT id FROM m)
          AND created_at > now() - interval '30 days'
    )
    SELECT
        ( 0.40 * verification_freshness((SELECT verified_at FROM m),
                                        (SELECT claim_kind  FROM m),
                                        (SELECT max_staleness_days FROM m))
        + 0.25 * (SELECT safety FROM contradiction)
        + 0.20 * (SELECT score  FROM outcome)
        + 0.10 * source_tier_weight((SELECT source_type FROM m))
        + 0.05 * COALESCE((SELECT stability FROM m), 0.5)
        )::real
$$;

-- 10. Optimised batch trust-score recomputation (avoid per-row PL/pgSQL)
CREATE OR REPLACE FUNCTION recompute_trust_scores(batch_size integer DEFAULT 5000)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    updated_rows integer := 0;
BEGIN
    WITH batch AS (
        SELECT id FROM memories
         WHERE trust_score IS NULL
            OR updated_at > now() - interval '1 hour'
         ORDER BY updated_at DESC
         LIMIT batch_size
    )
    UPDATE memories m
       SET trust_score = compute_trust_score(m.id)
      FROM batch
     WHERE m.id = batch.id;
    GET DIAGNOSTICS updated_rows = ROW_COUNT;
    RETURN updated_rows;
END
$$;

-- 11. View: stale memories that need re-verification
CREATE OR REPLACE VIEW stale_memories AS
    SELECT m.id, m.claim_kind, m.verified_at,
           COALESCE(m.max_staleness_days, default_max_staleness_days(m.claim_kind)) AS staleness_days,
           m.access_count, m.tier, m.source_type,
           LEFT(m.summary, 120) AS summary_excerpt
      FROM memories m
     WHERE m.claim_kind IN ('code-state','infra-state','reference','fact')
       AND m.superseded_by IS NULL
       AND (m.verified_at IS NULL
            OR m.verified_at < now() - (
                COALESCE(m.max_staleness_days, default_max_staleness_days(m.claim_kind))
                * interval '1 day'
            ))
     ORDER BY m.access_count DESC NULLS LAST, m.tier ASC;

-- 12. View: contradiction conflicts (two memories that disagree, both still active)
CREATE OR REPLACE VIEW contradiction_conflicts AS
    SELECT a.id AS memory_id, a.summary AS summary_a,
           b.id AS contradicting_id, b.summary AS summary_b,
           a.created_at AS created_a, b.created_at AS created_b,
           a.verified_at AS verified_a, b.verified_at AS verified_b
      FROM memories a
      JOIN memories b ON a.contradicted_by = b.id
     WHERE a.superseded_by IS NULL;

COMMIT;

-- After commit, the operator (you, this script, the scheduler) is expected to:
--   1. Run the heuristic claim_kind backfill (see 2026-04-27-trust-backfill.py)
--   2. Seed verification_probes with starter probes (see verifications.yaml)
--   3. Run SELECT recompute_trust_scores(50000); to populate trust_score
--   4. Wire memory_search to surface trust_score in responses
