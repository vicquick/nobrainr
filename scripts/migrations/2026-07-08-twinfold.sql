-- Twinfold (2026-07-08): fold 1,907 alnum-twin entity groups (2,081
-- losers) into their highest-mention winner. Companion to PR #96 (which
-- stops NEW twins at insert). Non-destructive posture: full backups,
-- relations soft-invalidated, loser entities left unlinked for the
-- entity_pruning janitor rather than deleted here.

BEGIN;

CREATE TEMP TABLE twin_map AS
WITH ranked AS (
    SELECT id, entity_type, mention_count,
           regexp_replace(canonical_name, '[^a-z0-9]', '', 'g') AS ak,
           row_number() OVER w AS rn,
           first_value(id) OVER w AS winner_id
    FROM entities
    WINDOW w AS (
        PARTITION BY entity_type, regexp_replace(canonical_name, '[^a-z0-9]', '', 'g')
        ORDER BY mention_count DESC, id ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
)
SELECT id AS loser_id, winner_id FROM ranked WHERE rn > 1;

-- Backups
CREATE TABLE IF NOT EXISTS twinfold_bak_entities AS
    SELECT e.* FROM entities e JOIN twin_map t ON t.loser_id = e.id;
CREATE TABLE IF NOT EXISTS twinfold_bak_entity_memories AS
    SELECT em.* FROM entity_memories em JOIN twin_map t ON t.loser_id = em.entity_id;
CREATE TABLE IF NOT EXISTS twinfold_bak_relations AS
    SELECT r.* FROM entity_relations r
    JOIN twin_map t ON t.loser_id IN (r.source_entity_id, r.target_entity_id);

-- 1) Rewire memory links to the winner (dedup on the PK), drop loser links.
INSERT INTO entity_memories (memory_id, entity_id, role, confidence)
SELECT em.memory_id, t.winner_id, em.role, max(em.confidence)
FROM entity_memories em
JOIN twin_map t ON t.loser_id = em.entity_id
GROUP BY em.memory_id, t.winner_id, em.role
ON CONFLICT (memory_id, entity_id, role) DO NOTHING;

DELETE FROM entity_memories em USING twin_map t WHERE em.entity_id = t.loser_id;

-- 2) Re-emit loser-touching valid edges with winner ids (aggregated per
--    triple — same ON CONFLICT pre-aggregation lesson as P2a), skip
--    self-loops, then soft-invalidate the originals.
INSERT INTO entity_relations
    (source_entity_id, target_entity_id, relationship_type,
     confidence, source_memory, properties, valid)
SELECT ns, nt, rt, max(conf),
       (array_agg(sm) FILTER (WHERE sm IS NOT NULL))[1],
       '{"twinfold": true}'::jsonb, true
FROM (
    SELECT COALESCE(ts.winner_id, r.source_entity_id) AS ns,
           COALESCE(tt.winner_id, r.target_entity_id) AS nt,
           r.relationship_type AS rt, r.confidence AS conf,
           r.source_memory AS sm
    FROM entity_relations r
    LEFT JOIN twin_map ts ON ts.loser_id = r.source_entity_id
    LEFT JOIN twin_map tt ON tt.loser_id = r.target_entity_id
    WHERE r.valid = true
      AND (ts.loser_id IS NOT NULL OR tt.loser_id IS NOT NULL)
) x
WHERE ns <> nt
GROUP BY ns, nt, rt
ON CONFLICT (source_entity_id, target_entity_id, relationship_type) DO UPDATE
    SET confidence = GREATEST(entity_relations.confidence, EXCLUDED.confidence);

UPDATE entity_relations r
SET valid = false,
    properties = COALESCE(r.properties, '{}'::jsonb) || '{"twinfold_invalidated": true}'::jsonb
FROM twin_map t
WHERE r.valid = true
  AND (r.source_entity_id = t.loser_id OR r.target_entity_id = t.loser_id);

-- 3) Move mention weight onto the winner.
UPDATE entities w
SET mention_count = w.mention_count + l.total
FROM (
    SELECT t.winner_id, sum(e.mention_count) AS total
    FROM entities e JOIN twin_map t ON t.loser_id = e.id
    GROUP BY t.winner_id
) l
WHERE w.id = l.winner_id;

SELECT (SELECT count(*) FROM twin_map) AS losers_folded,
       (SELECT count(*) FROM entity_memories em JOIN twin_map t ON t.loser_id = em.entity_id) AS loser_links_left,
       (SELECT count(*) FROM entity_relations WHERE valid) AS valid_relations,
       (SELECT count(*) FROM twinfold_bak_entities) AS backed_up_entities;

COMMIT;
