-- P2a migration (2026-07-07): fold the freeform relationship_type tail
-- onto the closed vocabulary. NON-DESTRUCTIVE: originals are soft-
-- invalidated (valid=false), canonical triples are upsert-merged.
-- Backup first; generated from extraction/models.py _REL_SYNONYMS.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_relations_bak_p2a AS
SELECT * FROM entity_relations
WHERE relationship_type NOT IN ('uses', 'depends_on', 'fixes', 'part_of', 'created_by', 'deployed_on', 'configured_with', 'replaces', 'conflicts_with', 'runs_on', 'implements', 'causes', 'tests');

CREATE TEMP TABLE rel_map (freeform text PRIMARY KEY, canonical text, swap boolean);
INSERT INTO rel_map VALUES
    ('used_by', 'uses', true),
    ('used_in', 'uses', true),
    ('relies_on', 'depends_on', false),
    ('requires', 'depends_on', false),
    ('required_by', 'depends_on', true),
    ('implemented_by', 'implements', true),
    ('implemented_in', 'implements', true),
    ('caused_by', 'causes', true),
    ('creates', 'created_by', true),
    ('contains', 'part_of', true),
    ('includes', 'part_of', true),
    ('member_of', 'part_of', false),
    ('belongs_to', 'part_of', false),
    ('configures', 'configured_with', true),
    ('supports', 'uses', true),
    ('handles', 'uses', false),
    ('tested_by', 'tests', true),
    ('replaced_by', 'replaces', true),
    ('superseded_by', 'replaces', true),
    ('supersedes', 'replaces', false),
    ('deprecates', 'replaces', false),
    ('deprecated_in', 'replaces', true),
    ('runs', 'runs_on', true),
    ('hosts', 'deployed_on', true),
    ('hosted_on', 'deployed_on', false),
    ('deployed_to', 'deployed_on', false),
    ('fixed_by', 'fixes', true),
    ('resolves', 'fixes', false),
    ('solved_by', 'fixes', true),
    ('conflicts', 'conflicts_with', false),
    ('integrates_with', 'uses', false),
    ('connects_to', 'uses', false);

-- 1) Re-emit mapped rows as canonical triples (swapped where inverse).
INSERT INTO entity_relations
    (source_entity_id, target_entity_id, relationship_type,
     confidence, source_memory, properties, valid)
SELECT x.src, x.tgt, x.canonical,
       max(x.confidence),
       (array_agg(x.source_memory) FILTER (WHERE x.source_memory IS NOT NULL))[1],
       jsonb_build_object('p2a_from', string_agg(DISTINCT x.orig_type, ',')),
       true
FROM (
    SELECT
        CASE WHEN m.swap THEN r.target_entity_id ELSE r.source_entity_id END AS src,
        CASE WHEN m.swap THEN r.source_entity_id ELSE r.target_entity_id END AS tgt,
        m.canonical,
        r.confidence,
        r.source_memory,
        r.relationship_type AS orig_type
    FROM entity_relations r
    JOIN rel_map m ON m.freeform = lower(replace(replace(r.relationship_type, ' ', '_'), '-', '_'))
    WHERE r.valid = true
      AND r.source_entity_id <> r.target_entity_id
) x
GROUP BY x.src, x.tgt, x.canonical
ON CONFLICT (source_entity_id, target_entity_id, relationship_type) DO UPDATE
    SET confidence = GREATEST(entity_relations.confidence, EXCLUDED.confidence);

-- 2) Soft-invalidate every non-canonical original.
UPDATE entity_relations
SET valid = false,
    properties = COALESCE(properties, '{}'::jsonb) || '{"p2a_invalidated": true}'::jsonb
WHERE relationship_type NOT IN ('uses', 'depends_on', 'fixes', 'part_of', 'created_by', 'deployed_on', 'configured_with', 'replaces', 'conflicts_with', 'runs_on', 'implements', 'causes', 'tests')
  AND valid = true;

SELECT (SELECT count(*) FROM entity_relations WHERE valid) AS valid_relations,
       (SELECT count(DISTINCT relationship_type) FROM entity_relations WHERE valid) AS distinct_types,
       (SELECT count(*) FROM entity_relations_bak_p2a) AS backed_up;

COMMIT;
