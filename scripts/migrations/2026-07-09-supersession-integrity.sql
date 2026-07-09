-- Supersession integrity + plan claim-kind (2026-07-09).
-- 1) Backfill 383 broken chains: memories whose metadata.supersedes was
--    recorded but whose target's superseded_by COLUMN was never set
--    (queued-write path bug — see services/memory.py fix).
-- 2) Extend claim_kind CHECK with 'plan': prescriptive future intent is
--    its own staleness class — unverifiable by probes, uncontradicted by
--    reality, reconciled only by the sweeper.

BEGIN;

-- 1) Backfill: newer memory M declares metadata.supersedes = X → X.superseded_by = M.id
WITH chains AS (
    SELECT m.id AS new_id,
           (m.metadata->>'supersedes')::uuid AS old_id,
           m.created_at
    FROM memories m
    WHERE m.metadata ? 'supersedes'
      AND m.metadata->>'supersedes' ~ '^[0-9a-f-]{36}$'
),
best AS (
    -- if several memories claim to supersede the same target, newest wins
    SELECT DISTINCT ON (old_id) old_id, new_id
    FROM chains ORDER BY old_id, created_at DESC
)
UPDATE memories t
SET superseded_by = b.new_id,
    updated_at = now(),
    metadata = COALESCE(t.metadata, '{}'::jsonb)
        || jsonb_build_object('superseded_by', b.new_id::text,
                              'superseded_reason', 'backfill 2026-07-09')
FROM best b
WHERE t.id = b.old_id
  AND t.superseded_by IS NULL
  AND t.id <> b.new_id;

-- 2) plan claim kind
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_claim_kind_check;
ALTER TABLE memories ADD CONSTRAINT memories_claim_kind_check CHECK (
    claim_kind IS NULL OR claim_kind = ANY (ARRAY[
        'code-state', 'infra-state', 'preference', 'incident-fix',
        'design-decision', 'historical', 'reference', 'fact', 'plan'
    ])
);

SELECT (SELECT count(*) FROM memories WHERE superseded_by IS NOT NULL) AS chains_now,
       (SELECT count(*) FROM memories WHERE metadata ? 'supersedes') AS claiming;

COMMIT;
