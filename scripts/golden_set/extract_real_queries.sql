-- Extract candidate real-life queries from memory_outcomes (the table that
-- captures was_useful feedback with the query that produced the result).
-- This gives us ground-truth labels for free: queries the user explicitly
-- clicked "useful" on imply that retrieved memory was a top-3 match.
--
-- Output: top 50 distinct queries with the memory_ids users marked useful.

SELECT
    rank() OVER (ORDER BY n_uses DESC, last_used DESC) AS rank,
    n_uses,
    last_used::date AS last_date,
    LEFT(query_text, 100) AS query,
    array_agg(useful_memory_id) FILTER (WHERE useful_memory_id IS NOT NULL) AS useful_mem_ids
  FROM (
    SELECT
        query_text,
        max(created_at)         AS last_used,
        count(*)                 AS n_uses,
        CASE WHEN was_useful THEN memory_id::text ELSE NULL END AS useful_memory_id
      FROM memory_outcomes
     WHERE query_text IS NOT NULL
       AND char_length(query_text) BETWEEN 5 AND 200
     GROUP BY query_text, was_useful, memory_id
  ) deduped
 GROUP BY query_text, n_uses, last_used
 ORDER BY n_uses DESC, last_used DESC
 LIMIT 50;
