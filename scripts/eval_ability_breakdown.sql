-- Per-ability breakdown of the latest eval run (2026-07-05).
-- Joins eval_runs.per_query (jsonb) back to golden tags so regressions
-- can be attributed to an ability type, not just the mean.
WITH latest AS (
    SELECT id, ran_at, per_query FROM eval_runs ORDER BY ran_at DESC LIMIT 1
),
pq AS (
    SELECT l.ran_at,
           (e->>'query_id')::uuid AS query_id,
           (e->>'recall_at_k')::float AS recall,
           (e->>'reciprocal_rank')::float AS rr,
           (e->>'ndcg_at_k')::float AS ndcg
    FROM latest l, jsonb_array_elements(l.per_query) e
)
SELECT COALESCE(g.tags[2], 'v1-original') AS ability,
       count(*) AS n,
       round(avg(pq.recall)::numeric, 3) AS recall_at_10,
       round(avg(pq.rr)::numeric, 3) AS mrr,
       round(avg(pq.ndcg)::numeric, 3) AS ndcg,
       count(*) FILTER (WHERE pq.recall = 0) AS total_misses
FROM pq
JOIN eval_golden_queries g ON g.id = pq.query_id
GROUP BY 1 ORDER BY recall_at_10;
