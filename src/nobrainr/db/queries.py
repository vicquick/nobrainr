"""Database query functions for memories, entities, and the knowledge graph."""

import json
import logging
from uuid import UUID

import numpy as np

from nobrainr.db.pool import get_pool
from nobrainr.events import publish

logger = logging.getLogger("nobrainr")


# ──────────────────────────────────────────────
# Memory CRUD
# ──────────────────────────────────────────────

async def store_memory(
    content: str,
    embedding: list[float],
    *,
    summary: str | None = None,
    source_type: str = "manual",
    source_machine: str | None = None,
    source_ref: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, summary, embedding, source_type, source_machine,
                                  source_ref, tags, category, confidence, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            RETURNING id, created_at
            """,
            content,
            summary,
            np.array(embedding, dtype=np.float32),
            source_type,
            source_machine,
            source_ref,
            tags or [],
            category,
            confidence,
            _jsonb(metadata),
        )
        result = {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}
        publish("memory_created", {"id": result["id"]})

        # Record version 0 (creation snapshot)
        try:
            await conn.execute(
                """INSERT INTO memory_versions
                   (memory_id, version, content, summary, tags, category, confidence,
                    metadata, change_type, changed_by)
                   VALUES ($1, 0, $2, $3, $4, $5, $6, $7, 'created', 'system')""",
                row["id"],
                content,
                summary,
                tags or [],
                category,
                confidence,
                _jsonb(metadata),
            )
        except Exception:
            logger.warning("Failed to record version 0 for %s", result["id"])

        return result


async def find_similar_memories(
    embedding: list[float],
    *,
    limit: int = 5,
    threshold: float = 0.85,
    exclude_id: str | None = None,
) -> list[dict]:
    """Find memories similar to the given embedding (for dedup checks)."""
    pool = await get_pool()
    vec = np.array(embedding, dtype=np.float32)
    async with pool.acquire() as conn:
        if exclude_id:
            rows = await conn.fetch(
                """
                SELECT id, content, summary, tags, category,
                       1 - (embedding <=> $1) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL AND id != $3
                ORDER BY embedding <=> $1
                LIMIT $2
                """,
                vec, limit, UUID(exclude_id),
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, content, summary, tags, category,
                       1 - (embedding <=> $1) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1
                LIMIT $2
                """,
                vec, limit,
            )
        return [
            _row_to_dict(row)
            for row in rows
            if row["similarity"] >= threshold
        ]


async def search_memories(
    embedding: list[float],
    *,
    limit: int = 10,
    threshold: float = 0.3,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    text_query: str | None = None,
) -> list[dict]:
    pool = await get_pool()
    vec = np.array(embedding, dtype=np.float32)

    # If hybrid mode with text_query: run RRF fusion of vector + FTS
    if text_query:
        return await _hybrid_search_rrf(
            pool, vec, text_query,
            limit=limit, threshold=threshold,
            tags=tags, category=category,
            source_type=source_type, source_machine=source_machine,
        )

    # Standard vector search with threshold in SQL WHERE
    conditions = ["embedding IS NOT NULL"]
    params: list = [vec, threshold, limit]
    idx = 4

    if tags:
        conditions.append(f"tags && ${idx}::text[]")
        params.append(tags)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if source_type:
        conditions.append(f"source_type = ${idx}")
        params.append(source_type)
        idx += 1
    if source_machine:
        conditions.append(f"source_machine = ${idx}")
        params.append(source_machine)
        idx += 1

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   access_count, last_accessed_at, quality_score,
                   1 - (embedding <=> $1) AS similarity,
                   memory_relevance($1, embedding, created_at, importance, stability, access_count, now(), quality_score) AS relevance
            FROM memories
            WHERE {where}
              AND 1 - (embedding <=> $1) >= $2
            ORDER BY memory_relevance($1, embedding, created_at, importance, stability, access_count, now(), quality_score) DESC
            LIMIT $3
            """,
            *params,
        )
        results = [_row_to_dict(row) for row in rows]

        if results:
            result_ids = [UUID(r["id"]) for r in results]
            await conn.execute(
                """
                UPDATE memories
                SET last_accessed_at = now(),
                    access_count = access_count + 1
                WHERE id = ANY($1)
                """,
                result_ids,
            )

        return results


def _build_filter_clause(
    start_idx: int,
    tags: list[str] | None,
    category: str | None,
    source_type: str | None,
    source_machine: str | None,
) -> tuple[str, list, int]:
    """Build shared WHERE filter fragment for hybrid search sub-queries."""
    conditions = []
    params = []
    idx = start_idx
    if tags:
        conditions.append(f"tags && ${idx}::text[]")
        params.append(tags)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if source_type:
        conditions.append(f"source_type = ${idx}")
        params.append(source_type)
        idx += 1
    if source_machine:
        conditions.append(f"source_machine = ${idx}")
        params.append(source_machine)
        idx += 1
    clause = (" AND " + " AND ".join(conditions)) if conditions else ""
    return clause, params, idx


async def _hybrid_search_rrf(
    pool,
    embedding,
    text_query: str,
    *,
    limit: int = 10,
    threshold: float = 0.3,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    rrf_k: int = 60,
) -> list[dict]:
    """Hybrid search using Reciprocal Rank Fusion of vector + full-text results."""
    overfetch = max(limit * 3, 20)

    async with pool.acquire() as conn:
        # 1) Vector search: $1=embedding, $2=threshold, $3=overfetch, filters from $4+
        vec_extra, vec_fparams, _ = _build_filter_clause(
            4, tags, category, source_type, source_machine,
        )
        vec_rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   access_count, last_accessed_at, quality_score,
                   1 - (embedding <=> $1) AS similarity,
                   memory_relevance($1, embedding, created_at, importance, stability, access_count, now(), quality_score) AS relevance
            FROM memories
            WHERE embedding IS NOT NULL
              AND 1 - (embedding <=> $1) >= $2
              {vec_extra}
            ORDER BY memory_relevance($1, embedding, created_at, importance, stability, access_count, now(), quality_score) DESC
            LIMIT $3
            """,
            embedding, threshold, overfetch, *vec_fparams,
        )

        # 2) Full-text search: $1=query, $2=overfetch, filters from $3+
        fts_extra, fts_fparams, _ = _build_filter_clause(
            3, tags, category, source_type, source_machine,
        )
        fts_rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   access_count, last_accessed_at, quality_score,
                   ts_rank(to_tsvector('english', content), plainto_tsquery('english', $1)) AS fts_rank
            FROM memories
            WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1)
              {fts_extra}
            ORDER BY fts_rank DESC
            LIMIT $2
            """,
            text_query, overfetch, *fts_fparams,
        )

        # 3) Reciprocal Rank Fusion
        rrf_scores: dict[str, float] = {}
        rows_by_id: dict[str, object] = {}

        for rank, row in enumerate(vec_rows, start=1):
            rid = str(row["id"])
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (rrf_k + rank)
            rows_by_id[rid] = row

        for rank, row in enumerate(fts_rows, start=1):
            rid = str(row["id"])
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (rrf_k + rank)
            if rid not in rows_by_id:
                rows_by_id[rid] = row

        sorted_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:limit]

        results = []
        for rid in sorted_ids:
            row = rows_by_id[rid]
            d = _row_to_dict(row)
            d["rrf_score"] = rrf_scores[rid]
            results.append(d)

        if results:
            result_ids = [UUID(r["id"]) for r in results]
            await conn.execute(
                """
                UPDATE memories
                SET last_accessed_at = now(),
                    access_count = access_count + 1
                WHERE id = ANY($1)
                """,
                result_ids,
            )

        return results


async def get_memory(memory_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, content, summary, source_type, source_machine, source_ref,
                   tags, category, confidence, metadata, created_at, updated_at,
                   importance, stability, access_count, last_accessed_at, extraction_status,
                   quality_score, quality_specificity, quality_actionability, quality_self_containment
            FROM memories WHERE id = $1
            """,
            UUID(memory_id),
        )
        if row:
            # Track access
            await conn.execute(
                "UPDATE memories SET last_accessed_at = now(), access_count = access_count + 1 WHERE id = $1",
                UUID(memory_id),
            )
            return _row_to_dict(row)
        return None


async def update_memory(
    memory_id: str,
    *,
    content: str | None = None,
    summary: str | None = None,
    embedding: list[float] | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    confidence: float | None = None,
    metadata: dict | None = None,
) -> dict | None:
    pool = await get_pool()
    sets = []
    params = []
    idx = 1

    for field, value in [
        ("content", content),
        ("summary", summary),
        ("tags", tags),
        ("category", category),
        ("confidence", confidence),
    ]:
        if value is not None:
            sets.append(f"{field} = ${idx}")
            params.append(value)
            idx += 1

    if embedding is not None:
        sets.append(f"embedding = ${idx}")
        params.append(np.array(embedding, dtype=np.float32))
        idx += 1

    if metadata is not None:
        sets.append(f"metadata = metadata || ${idx}::jsonb")
        params.append(_jsonb(metadata))
        idx += 1

    if not sets:
        return await get_memory(memory_id)

    params.append(UUID(memory_id))
    set_clause = ", ".join(sets)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE memories SET {set_clause}
            WHERE id = ${idx}
            RETURNING id, content, summary, source_type, source_machine,
                      tags, category, confidence, metadata, created_at, updated_at
            """,
            *params,
        )
        result = _row_to_dict(row) if row else None
        if result:
            publish("memory_updated", {"id": memory_id})
        return result


async def delete_memory(memory_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM memories WHERE id = $1",
            UUID(memory_id),
        )
        deleted = result == "DELETE 1"
        if deleted:
            publish("memory_deleted", {"id": memory_id})
        return deleted


# ──────────────────────────────────────────────
# Memory versioning (audit trail / time machine)
# ──────────────────────────────────────────────

async def record_memory_version(
    memory_id: str,
    change_type: str,
    *,
    change_reason: str | None = None,
    changed_by: str | None = None,
    source_memory_id: str | None = None,
    similarity_score: float | None = None,
    old_snapshot: dict | None = None,
) -> int | None:
    """Snapshot a memory's current state as a version record.

    Call this BEFORE mutating the memory. For 'created' events, pass the
    new content as old_snapshot since the memory doesn't exist yet.

    Returns the version number, or None if the memory wasn't found.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get current state (or use provided snapshot for creation)
        if old_snapshot is not None:
            mem = old_snapshot
        else:
            row = await conn.fetchrow(
                "SELECT content, summary, tags, category, confidence, metadata "
                "FROM memories WHERE id = $1",
                UUID(memory_id),
            )
            if not row:
                return None
            mem = dict(row)

        # Next version number
        max_ver = await conn.fetchval(
            "SELECT COALESCE(MAX(version), -1) FROM memory_versions WHERE memory_id = $1",
            UUID(memory_id),
        )
        version = max_ver + 1

        await conn.execute(
            """INSERT INTO memory_versions
               (memory_id, version, content, summary, tags, category, confidence,
                metadata, change_type, change_reason, changed_by,
                source_memory_id, similarity_score)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
            UUID(memory_id),
            version,
            mem.get("content", ""),
            mem.get("summary"),
            mem.get("tags") or [],
            mem.get("category"),
            mem.get("confidence"),
            json.dumps(mem.get("metadata") or {}),
            change_type,
            change_reason,
            changed_by,
            UUID(source_memory_id) if source_memory_id else None,
            similarity_score,
        )
        return version


async def get_memory_history(memory_id: str) -> list[dict]:
    """Get full version history for a memory, newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, memory_id, version, content, summary, tags, category,
                      confidence, metadata, change_type, change_reason,
                      changed_by, source_memory_id, similarity_score,
                      content_changed, tags_changed, category_changed, created_at
               FROM memory_versions
               WHERE memory_id = $1
               ORDER BY version DESC""",
            UUID(memory_id),
        )
        return [
            {
                **dict(r),
                "id": str(r["id"]),
                "memory_id": str(r["memory_id"]),
                "source_memory_id": str(r["source_memory_id"]) if r["source_memory_id"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            }
            for r in rows
        ]


async def restore_memory_version(memory_id: str, version: int) -> dict | None:
    """Restore a memory to a specific version. Records a 'restore' version."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get the target version snapshot
        row = await conn.fetchrow(
            """SELECT content, summary, tags, category, confidence, metadata
               FROM memory_versions
               WHERE memory_id = $1 AND version = $2""",
            UUID(memory_id),
            version,
        )
        if not row:
            return None

        snapshot = dict(row)

        # Record current state before restoring
        await record_memory_version(
            memory_id,
            "restore",
            change_reason=f"Restored to version {version}",
            changed_by="manual",
        )

        # Apply the snapshot
        new_embedding = None
        try:
            from nobrainr.embeddings.ollama import embed_text
            new_embedding = await embed_text(snapshot["content"])
        except Exception:
            pass

        await conn.execute(
            """UPDATE memories
               SET content = $2, summary = $3, tags = $4, category = $5,
                   confidence = $6, metadata = $7::jsonb,
                   embedding = COALESCE($8, embedding)
               WHERE id = $1""",
            UUID(memory_id),
            snapshot["content"],
            snapshot["summary"],
            snapshot["tags"] or [],
            snapshot["category"],
            snapshot["confidence"],
            json.dumps(json.loads(snapshot["metadata"]) if snapshot["metadata"] else {}),
            new_embedding,
        )

        publish("memory_updated", {"id": memory_id, "restored_to_version": version})
        return {"id": memory_id, "restored_to_version": version, "content": snapshot["content"]}


async def query_memories(
    *,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    text_query: str | None = None,
    min_quality: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    pool = await get_pool()
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if tags:
        conditions.append(f"tags && ${idx}::text[]")
        params.append(tags)
        idx += 1

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    if source_type:
        conditions.append(f"source_type = ${idx}")
        params.append(source_type)
        idx += 1

    if source_machine:
        conditions.append(f"source_machine = ${idx}")
        params.append(source_machine)
        idx += 1

    if text_query:
        conditions.append(f"to_tsvector('english', content) @@ plainto_tsquery('english', ${idx})")
        params.append(text_query)
        idx += 1

    if min_quality is not None:
        conditions.append(f"quality_score >= ${idx}")
        params.append(min_quality)
        idx += 1

    params.extend([limit, offset])
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   quality_score
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


# ──────────────────────────────────────────────
# Memory intelligence
# ──────────────────────────────────────────────

async def recompute_importance() -> int:
    """Recompute importance using graph-structural signals + quality score."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memories m SET importance = LEAST(1.0,
                -- 40% entity connectivity (how many entities this memory links to, normalized)
                (0.4 * LEAST(1.0, COALESCE((
                    SELECT count(*)::real / 10.0
                    FROM entity_memories em WHERE em.memory_id = m.id
                ), 0.0)))
                -- 30% quality score (LLM-assessed, default 0.5 if not scored)
              + (0.3 * COALESCE(quality_score, 0.5))
                -- 30% confidence (source reliability)
              + (0.3 * COALESCE(confidence, 0.7))
            )
            WHERE embedding IS NOT NULL
            """
        )
        return int(result.split()[-1]) if result else 0


async def decay_stability() -> int:
    """Decay stability for memories not accessed in 7+ days. Returns count updated."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memories
            SET stability = GREATEST(0.1, stability * 0.95)
            WHERE (last_accessed_at IS NULL AND created_at < now() - interval '7 days')
               OR (last_accessed_at < now() - interval '7 days')
            """
        )
        return int(result.split()[-1]) if result else 0


async def normalize_categories(category_map: dict[str, str]) -> int:
    """Bulk-normalize memory categories using a mapping dict. Returns count updated."""
    pool = await get_pool()
    total = 0
    async with pool.acquire() as conn:
        for old_cat, new_cat in category_map.items():
            if old_cat == new_cat:
                continue
            result = await conn.execute(
                "UPDATE memories SET category = $1 WHERE category = $2",
                new_cat, old_cat,
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                total += count
                logger.info("Normalized category '%s' → '%s': %d memories", old_cat, new_cat, count)
    return total


async def analyze_tables() -> None:
    """Run ANALYZE on core tables to refresh planner statistics."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("ANALYZE memories")
        await conn.execute("ANALYZE entities")
        await conn.execute("ANALYZE entity_memories")
        await conn.execute("ANALYZE entity_relations")


async def store_memory_outcome(
    memory_id: str,
    was_useful: bool,
    *,
    context: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Record feedback on whether a memory search result was useful."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory_outcomes (memory_id, was_useful, context, agent_id, session_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, created_at
            """,
            UUID(memory_id), was_useful, context, agent_id, session_id,
        )
        publish("feedback_added", {"memory_id": memory_id, "was_useful": was_useful})
        return {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}


async def integrate_feedback_scores() -> int:
    """Adjust importance based on memory_outcomes feedback. Needs 2+ entries per memory.
    Positive ratio adjusts importance by ±0.1 max. Returns count updated."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memories m
            SET importance = LEAST(1.0, GREATEST(0.0,
                m.importance + (
                    CASE
                        WHEN fb.positive_ratio >= 0.5 THEN LEAST(0.1, (fb.positive_ratio - 0.5) * 0.2)
                        ELSE GREATEST(-0.1, (fb.positive_ratio - 0.5) * 0.2)
                    END
                )
            ))
            FROM (
                SELECT memory_id,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE was_useful) ::real / COUNT(*) AS positive_ratio
                FROM memory_outcomes
                GROUP BY memory_id
                HAVING COUNT(*) >= 2
            ) fb
            WHERE m.id = fb.memory_id
            """
        )
        return int(result.split()[-1]) if result else 0


async def log_agent_event(
    event_type: str,
    description: str,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    related_memory_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Log an agent activity event."""
    pool = await get_pool()
    mem_ids = [UUID(mid) for mid in related_memory_ids] if related_memory_ids else None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_events (event_type, description, agent_id, session_id,
                                      category, related_memory_ids, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id, created_at
            """,
            event_type, description, agent_id, session_id,
            category, mem_ids, _jsonb(metadata),
        )
        publish("agent_event", {"event_type": event_type, "id": str(row["id"])})
        return {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}


async def log_scheduler_event(job_name: str, result: dict) -> None:
    """Log a scheduler job execution as an agent event."""
    await log_agent_event(
        event_type="scheduler",
        description=f"Scheduled job '{job_name}' completed",
        agent_id="scheduler",
        category="system",
        metadata={"job": job_name, "result": result},
    )


async def get_unsummarized_memories(limit: int = 10) -> list[dict]:
    """Get memories with no summary and content longer than 50 chars."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content
            FROM memories
            WHERE summary IS NULL AND LENGTH(content) > 50
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(row) for row in rows]


async def get_similar_memory_pairs(
    threshold: float = 0.88,
    limit: int = 5,
) -> list[dict]:
    """Find memory pairs with high cosine similarity that haven't been consolidation-checked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id AS id_a, a.content AS content_a,
                   b.id AS id_b, b.content AS content_b,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM memories a
            JOIN memories b ON a.id < b.id
            WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
              AND 1 - (a.embedding <=> b.embedding) > $1
              AND NOT EXISTS (
                  SELECT 1 FROM agent_events
                  WHERE event_type = 'consolidation_checked'
                    AND metadata->>'id_a' = a.id::text
                    AND metadata->>'id_b' = b.id::text
              )
            ORDER BY (a.embedding <=> b.embedding) ASC
            LIMIT $2
            """,
            threshold, limit,
        )
        return [_row_to_dict(row) for row in rows]


async def mark_memories_consolidation_checked(id_a: str, id_b: str) -> None:
    """Mark a pair of memories as checked for consolidation."""
    await log_agent_event(
        event_type="consolidation_checked",
        description=f"Checked pair {str(id_a)[:8]}../{str(id_b)[:8]}.. for consolidation",
        agent_id="scheduler",
        category="system",
        metadata={"id_a": id_a, "id_b": id_b},
    )


async def get_synthesis_candidates(limit: int = 3) -> list[dict]:
    """Get entities with 3+ linked memories that haven't been synthesized in 7 days."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id AS entity_id, e.name AS entity_name, e.entity_type,
                   COUNT(em.memory_id) AS memory_count,
                   ARRAY_AGG(m.content ORDER BY m.created_at DESC) AS memory_contents,
                   ARRAY_AGG(m.id ORDER BY m.created_at DESC) AS memory_ids
            FROM entities e
            JOIN entity_memories em ON em.entity_id = e.id
            JOIN memories m ON m.id = em.memory_id
            WHERE NOT EXISTS (
                SELECT 1 FROM agent_events
                WHERE event_type = 'synthesis'
                  AND metadata->>'entity_id' = e.id::text
                  AND created_at > now() - interval '7 days'
            )
            GROUP BY e.id, e.name, e.entity_type
            HAVING COUNT(em.memory_id) >= 3
            ORDER BY COUNT(em.memory_id) DESC
            LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(row) for row in rows]


async def get_underdescribed_entities(limit: int = 10) -> list[dict]:
    """Get entities with no/short description and 2+ mentions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.name, e.entity_type, e.description, e.mention_count,
                   ARRAY_AGG(m.content ORDER BY m.created_at DESC) AS memory_contents
            FROM entities e
            JOIN entity_memories em ON em.entity_id = e.id
            JOIN memories m ON m.id = em.memory_id
            WHERE (e.description IS NULL OR LENGTH(e.description) < 20)
              AND e.mention_count >= 2
            GROUP BY e.id, e.name, e.entity_type, e.description, e.mention_count
            ORDER BY e.mention_count DESC
            LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(row) for row in rows]


async def update_entity_description(entity_id: str, description: str) -> None:
    """Update an entity's description."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE entities SET description = $1 WHERE id = $2",
            description, UUID(entity_id),
        )


async def get_unprocessed_events(limit: int = 20) -> list[dict]:
    """Get agent events not yet processed for insight extraction."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, event_type, description, metadata, created_at, agent_id, category
            FROM agent_events
            WHERE event_type IN ('decision', 'error', 'task_complete', 'session_end')
              AND NOT EXISTS (
                  SELECT 1 FROM agent_events ae2
                  WHERE ae2.event_type = 'insight_processed'
                    AND ae2.metadata->>'source_event_id' = agent_events.id::text
              )
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(row) for row in rows]


async def mark_event_processed(event_id: str) -> None:
    """Mark an agent event as processed for insight extraction."""
    await log_agent_event(
        event_type="insight_processed",
        description=f"Processed event {event_id[:8]}.. for insights",
        agent_id="scheduler",
        category="system",
        metadata={"source_event_id": event_id},
    )


async def archive_stale_memories(limit: int = 50) -> int:
    """Archive low-value, never-accessed memories older than 30 days."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE memories
            SET category = '_archived'
            WHERE id IN (
                SELECT id FROM memories
                WHERE stability < 0.3
                  AND importance < 0.2
                  AND access_count = 0
                  AND category != '_archived'
                  AND source_type NOT IN ('synthesis', 'insight', 'agent_learning')
                  AND created_at < now() - interval '30 days'
                ORDER BY importance ASC, stability ASC
                LIMIT $1
            )
        """, limit)
        count = int(result.split()[-1])
        return count


async def get_unscored_memories(limit: int = 20) -> list[dict]:
    """Get memories that haven't been quality-scored yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, summary, source_type, category, tags
            FROM memories
            WHERE quality_score IS NULL
              AND category != '_archived'
              AND content IS NOT NULL
              AND length(content) > 20
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(row) for row in rows]


async def update_quality_score(
    memory_id: str,
    *,
    quality_score: float,
    specificity: int,
    actionability: int,
    self_containment: int,
) -> None:
    """Update quality scores for a memory."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET quality_score = $2,
                quality_specificity = $3,
                quality_actionability = $4,
                quality_self_containment = $5
            WHERE id = $1
            """,
            UUID(memory_id),
            quality_score,
            specificity,
            actionability,
            self_containment,
        )


async def get_potential_contradictions(limit: int = 5) -> list[dict]:
    """Find high-similarity memory pairs from different sources that haven't been checked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH candidates AS (
                SELECT
                    m1.id as id_a, m2.id as id_b,
                    m1.content as content_a, m2.content as content_b,
                    m1.source_machine as machine_a, m2.source_machine as machine_b,
                    1 - (m1.embedding <=> m2.embedding) as similarity
                FROM memories m1
                JOIN memories m2 ON m1.id < m2.id
                WHERE m1.embedding IS NOT NULL
                  AND m2.embedding IS NOT NULL
                  AND m1.category != '_archived'
                  AND m2.category != '_archived'
                  AND 1 - (m1.embedding <=> m2.embedding) BETWEEN 0.75 AND 0.92
                  AND (m1.source_machine != m2.source_machine
                       OR m1.source_type != m2.source_type
                       OR m1.created_at < m2.created_at - interval '7 days')
                  AND NOT EXISTS (
                      SELECT 1 FROM memories m3
                      WHERE m3.source_type = 'contradiction'
                        AND m3.metadata->>'memory_a' = m1.id::text
                        AND m3.metadata->>'memory_b' = m2.id::text
                  )
                ORDER BY similarity DESC
                LIMIT $1
            )
            SELECT * FROM candidates
        """, limit)
        return [dict(r) for r in rows]


async def set_extraction_status(memory_id: str, status: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET extraction_status = $1 WHERE id = $2",
            status, UUID(memory_id),
        )


async def get_unextracted_memories(batch_size: int = 5) -> list[dict]:
    """Get memories that haven't been extracted yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, summary, tags, category
            FROM memories
            WHERE extraction_status IS NULL OR extraction_status = 'failed'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            batch_size,
        )
        return [_row_to_dict(row) for row in rows]


# ──────────────────────────────────────────────
# Entity CRUD
# ──────────────────────────────────────────────

async def find_or_create_entity(
    name: str,
    entity_type: str,
    *,
    description: str | None = None,
    embedding: list[float] | None = None,
) -> str:
    """Find entity by canonical name or create it. Returns entity ID."""
    canonical = name.lower().strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Try to find existing
        row = await conn.fetchrow(
            "SELECT id FROM entities WHERE canonical_name = $1 AND entity_type = $2",
            canonical, entity_type,
        )
        if row:
            # Bump mention count
            await conn.execute(
                "UPDATE entities SET mention_count = mention_count + 1 WHERE id = $1",
                row["id"],
            )
            return str(row["id"])

        # Create new
        vec = np.array(embedding, dtype=np.float32) if embedding else None
        row = await conn.fetchrow(
            """
            INSERT INTO entities (name, entity_type, canonical_name, description, embedding)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (canonical_name, entity_type) DO UPDATE
                SET mention_count = entities.mention_count + 1
            RETURNING id
            """,
            name, entity_type, canonical, description, vec,
        )
        return str(row["id"])


async def link_entity_to_memory(
    memory_id: str,
    entity_id: str,
    role: str = "mention",
    confidence: float = 1.0,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity_memories (memory_id, entity_id, role, confidence)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (memory_id, entity_id, role) DO UPDATE
                SET confidence = EXCLUDED.confidence
            """,
            UUID(memory_id), UUID(entity_id), role, confidence,
        )


async def store_entity_relation(
    source_entity_id: str,
    target_entity_id: str,
    relationship_type: str,
    *,
    confidence: float = 1.0,
    source_memory: str | None = None,
    properties: dict | None = None,
) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO entity_relations
                (source_entity_id, target_entity_id, relationship_type,
                 confidence, source_memory, properties)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (source_entity_id, target_entity_id, relationship_type) DO UPDATE
                SET confidence = GREATEST(entity_relations.confidence, EXCLUDED.confidence),
                    source_memory = COALESCE(EXCLUDED.source_memory, entity_relations.source_memory)
            RETURNING id
            """,
            UUID(source_entity_id),
            UUID(target_entity_id),
            relationship_type,
            confidence,
            UUID(source_memory) if source_memory else None,
            _jsonb(properties),
        )
        return str(row["id"])


async def search_entities(
    embedding: list[float],
    *,
    entity_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Semantic search on entity embeddings."""
    pool = await get_pool()
    vec = np.array(embedding, dtype=np.float32)
    conditions = ["embedding IS NOT NULL"]
    params: list = [vec, limit]
    idx = 3

    if entity_type:
        conditions.append(f"entity_type = ${idx}")
        params.append(entity_type)

    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, name, entity_type, canonical_name, description,
                   mention_count, metadata, created_at, updated_at,
                   1 - (embedding <=> $1) AS similarity
            FROM entities
            WHERE {where}
            ORDER BY embedding <=> $1
            LIMIT $2
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


async def get_entity_graph(entity_name: str, depth: int = 2) -> dict:
    """Get entity and its connections via recursive CTE traversal."""
    canonical = entity_name.lower().strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Find starting entity
        start = await conn.fetchrow(
            "SELECT id, name, entity_type, description, mention_count FROM entities WHERE canonical_name = $1",
            canonical,
        )
        if not start:
            return {"nodes": [], "edges": []}

        start_id = start["id"]

        # Recursive traversal
        rows = await conn.fetch(
            """
            WITH RECURSIVE graph AS (
                -- Seed: direct connections from start entity
                SELECT
                    CASE WHEN er.source_entity_id = $1 THEN er.target_entity_id
                         ELSE er.source_entity_id END AS entity_id,
                    er.id AS relation_id,
                    er.source_entity_id,
                    er.target_entity_id,
                    er.relationship_type,
                    er.confidence,
                    1 AS depth
                FROM entity_relations er
                WHERE (er.source_entity_id = $1 OR er.target_entity_id = $1)
                  AND er.valid = true

                UNION

                -- Recurse: connections of connected entities
                SELECT
                    CASE WHEN er.source_entity_id = g.entity_id THEN er.target_entity_id
                         ELSE er.source_entity_id END,
                    er.id,
                    er.source_entity_id,
                    er.target_entity_id,
                    er.relationship_type,
                    er.confidence,
                    g.depth + 1
                FROM entity_relations er
                JOIN graph g ON (er.source_entity_id = g.entity_id OR er.target_entity_id = g.entity_id)
                WHERE g.depth < $2
                  AND er.valid = true
            )
            SELECT DISTINCT ON (g.relation_id)
                g.relation_id, g.source_entity_id, g.target_entity_id,
                g.relationship_type, g.confidence, g.depth,
                se.name AS source_name, se.entity_type AS source_type,
                te.name AS target_name, te.entity_type AS target_type
            FROM graph g
            JOIN entities se ON se.id = g.source_entity_id
            JOIN entities te ON te.id = g.target_entity_id
            ORDER BY g.relation_id, g.depth
            """,
            start_id, depth,
        )

        # Build nodes and edges
        nodes_map = {
            str(start_id): {
                "id": str(start_id),
                "name": start["name"],
                "entity_type": start["entity_type"],
                "description": start["description"],
                "mention_count": start["mention_count"],
            }
        }
        edges = []

        for row in rows:
            src_id = str(row["source_entity_id"])
            tgt_id = str(row["target_entity_id"])
            if src_id not in nodes_map:
                nodes_map[src_id] = {
                    "id": src_id,
                    "name": row["source_name"],
                    "entity_type": row["source_type"],
                }
            if tgt_id not in nodes_map:
                nodes_map[tgt_id] = {
                    "id": tgt_id,
                    "name": row["target_name"],
                    "entity_type": row["target_type"],
                }
            edges.append({
                "id": str(row["relation_id"]),
                "source": src_id,
                "target": tgt_id,
                "relationship_type": row["relationship_type"],
                "confidence": round(float(row["confidence"]), 2),
                "depth": row["depth"],
            })

        return {"nodes": list(nodes_map.values()), "edges": edges}


async def get_entity_memories(entity_id: str) -> list[dict]:
    """Get all memories linked to an entity."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, m.content, m.summary, m.tags, m.category,
                   m.created_at, m.updated_at, em.role, em.confidence
            FROM memories m
            JOIN entity_memories em ON em.memory_id = m.id
            WHERE em.entity_id = $1
            ORDER BY m.created_at DESC
            """,
            UUID(entity_id),
        )
        return [_row_to_dict(row) for row in rows]


async def get_memory_entities(memory_id: str) -> list[dict]:
    """Get all entities linked to a memory."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.name, e.canonical_name, e.entity_type,
                   e.description, em.role, em.confidence
            FROM entities e
            JOIN entity_memories em ON em.entity_id = e.id
            WHERE em.memory_id = $1
            ORDER BY em.confidence DESC
            """,
            UUID(memory_id),
        )
        return [_row_to_dict(row) for row in rows]


# ──────────────────────────────────────────────
# Dashboard / API queries
# ──────────────────────────────────────────────

async def get_all_entities_for_graph(*, min_connections: int = 0) -> dict:
    """Get all entities and relations for the full knowledge graph visualization.

    Optional min_connections filter (default 0 = show everything).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if min_connections > 0:
            entity_rows = await conn.fetch(
                """
                SELECT e.id, e.name, e.entity_type, e.canonical_name,
                       e.description, e.mention_count, e.created_at
                FROM entities e
                WHERE (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = e.id) >= $1
                ORDER BY e.mention_count DESC
                """,
                min_connections,
            )
        else:
            entity_rows = await conn.fetch(
                """
                SELECT id, name, entity_type, canonical_name, description,
                       mention_count, created_at
                FROM entities
                ORDER BY mention_count DESC
                """
            )
        # Only include relations where both endpoints are in the set
        entity_ids = [r["id"] for r in entity_rows]
        if entity_ids and min_connections > 0:
            relation_rows = await conn.fetch(
                """
                SELECT id, source_entity_id, target_entity_id,
                       relationship_type, confidence
                FROM entity_relations
                WHERE valid = true
                  AND source_entity_id = ANY($1)
                  AND target_entity_id = ANY($1)
                """,
                entity_ids,
            )
        else:
            relation_rows = await conn.fetch(
                """
                SELECT id, source_entity_id, target_entity_id,
                       relationship_type, confidence
                FROM entity_relations
                WHERE valid = true
                """
            )

    nodes = []
    for r in entity_rows:
        nodes.append({
            "data": {
                "id": str(r["id"]),
                "label": r["name"],
                "type": r["entity_type"],
                "description": r["description"] or "",
                "mention_count": r["mention_count"],
            }
        })

    edges = []
    for r in relation_rows:
        edges.append({
            "data": {
                "id": str(r["id"]),
                "source": str(r["source_entity_id"]),
                "target": str(r["target_entity_id"]),
                "label": r["relationship_type"],
                "confidence": round(float(r["confidence"]), 2),
            }
        })

    return {"nodes": nodes, "edges": edges}


async def get_duplicate_entities(limit: int = 10) -> list[dict]:
    """Find entity pairs that likely refer to the same thing.

    Catches:
    1. Same canonical_name but different entity_type (e.g. "docker" as technology AND concept)
    2. High embedding similarity between different entities (e.g. "PostgreSQL" vs "postgres")

    Returns pairs not yet checked (via agent_events).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Strategy 1: Same name, different type
        name_dupes = await conn.fetch(
            """
            SELECT a.id AS id_a, a.name AS name_a, a.entity_type AS type_a,
                   a.mention_count AS mentions_a,
                   (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = a.id) AS mem_count_a,
                   b.id AS id_b, b.name AS name_b, b.entity_type AS type_b,
                   b.mention_count AS mentions_b,
                   (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = b.id) AS mem_count_b,
                   1.0::float AS similarity
            FROM entities a
            JOIN entities b ON a.canonical_name = b.canonical_name AND a.id < b.id
            WHERE NOT EXISTS (
                SELECT 1 FROM agent_events
                WHERE event_type = 'entity_merge_checked'
                  AND metadata->>'id_a' = a.id::text
                  AND metadata->>'id_b' = b.id::text
            )
            ORDER BY a.mention_count + b.mention_count DESC
            LIMIT $1
            """,
            limit,
        )

        # Strategy 2: High embedding similarity (different names)
        remaining = limit - len(name_dupes)
        embed_dupes = []
        if remaining > 0:
            embed_dupes = await conn.fetch(
                """
                SELECT a.id AS id_a, a.name AS name_a, a.entity_type AS type_a,
                       a.mention_count AS mentions_a,
                       (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = a.id) AS mem_count_a,
                       b.id AS id_b, b.name AS name_b, b.entity_type AS type_b,
                       b.mention_count AS mentions_b,
                       (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = b.id) AS mem_count_b,
                       1 - (a.embedding <=> b.embedding) AS similarity
                FROM entities a
                JOIN entities b ON a.id < b.id
                WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                  AND a.canonical_name != b.canonical_name
                  AND 1 - (a.embedding <=> b.embedding) > 0.85
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_events
                      WHERE event_type = 'entity_merge_checked'
                        AND metadata->>'id_a' = a.id::text
                        AND metadata->>'id_b' = b.id::text
                  )
                ORDER BY (a.embedding <=> b.embedding) ASC
                LIMIT $1
                """,
                remaining,
            )

        return [_row_to_dict(r) for r in list(name_dupes) + list(embed_dupes)]


async def merge_entities(winner_id: str, loser_id: str) -> None:
    """Merge loser entity into winner: transfer all memory links and relations, then delete loser."""
    pool = await get_pool()
    w = UUID(winner_id)
    loser = UUID(loser_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Transfer entity_memories (skip duplicates)
            await conn.execute(
                """
                INSERT INTO entity_memories (entity_id, memory_id, role, confidence)
                SELECT $1, memory_id, role, confidence
                FROM entity_memories WHERE entity_id = $2
                ON CONFLICT DO NOTHING
                """,
                w, loser,
            )
            # Transfer relations (source side)
            await conn.execute(
                """
                UPDATE entity_relations SET source_entity_id = $1
                WHERE source_entity_id = $2
                AND NOT EXISTS (
                    SELECT 1 FROM entity_relations er2
                    WHERE er2.source_entity_id = $1
                      AND er2.target_entity_id = entity_relations.target_entity_id
                      AND er2.relationship_type = entity_relations.relationship_type
                )
                """,
                w, loser,
            )
            # Transfer relations (target side)
            await conn.execute(
                """
                UPDATE entity_relations SET target_entity_id = $1
                WHERE target_entity_id = $2
                AND NOT EXISTS (
                    SELECT 1 FROM entity_relations er2
                    WHERE er2.source_entity_id = entity_relations.source_entity_id
                      AND er2.target_entity_id = $1
                      AND er2.relationship_type = entity_relations.relationship_type
                )
                """,
                w, loser,
            )
            # Sum mention counts
            await conn.execute(
                """
                UPDATE entities SET mention_count = mention_count + (
                    SELECT mention_count FROM entities WHERE id = $2
                ) WHERE id = $1
                """,
                w, loser,
            )
            # Delete loser (CASCADE removes remaining orphaned links)
            await conn.execute("DELETE FROM entities WHERE id = $1", loser)


async def mark_entity_merge_checked(id_a: str, id_b: str) -> None:
    """Mark an entity pair as checked so we don't re-evaluate it."""
    await log_agent_event(
        event_type="entity_merge_checked",
        description=f"Checked entity pair {str(id_a)[:8]}../{str(id_b)[:8]}.. for merging",
        agent_id="scheduler",
        category="system",
        metadata={"id_a": str(id_a), "id_b": str(id_b)},
    )


async def prune_noise_entities(*, min_age_hours: int = 24) -> dict:
    """Delete low-value entities: linked to only 1 memory, older than min_age_hours.

    Single-memory entities that survive long enough without being linked to more
    memories are noise. Relations between noise entities are also noise — they were
    created in the same extraction pass and don't represent independent knowledge.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Delete entities linked to <=1 memory and older than threshold.
        # CASCADE on entity_relations will clean up associated relations.
        result = await conn.execute(
            """
            DELETE FROM entities
            WHERE id IN (
                SELECT e.id
                FROM entities e
                WHERE e.created_at < NOW() - make_interval(hours => $1)
                  AND (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = e.id) <= 1
            )
            """,
            min_age_hours,
        )
        pruned = int(result.split()[-1]) if result else 0

        # Also delete orphaned relations (safety net)
        orphan_result = await conn.execute(
            """
            DELETE FROM entity_relations
            WHERE NOT EXISTS (SELECT 1 FROM entities WHERE id = source_entity_id)
               OR NOT EXISTS (SELECT 1 FROM entities WHERE id = target_entity_id)
            """
        )
        orphan_relations = int(orphan_result.split()[-1]) if orphan_result else 0

    return {"entities_pruned": pruned, "orphan_relations_removed": orphan_relations}


async def get_timeline_memories(
    *,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    source_machine: str | None = None,
) -> list[dict]:
    pool = await get_pool()
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    if source_machine:
        conditions.append(f"source_machine = ${idx}")
        params.append(source_machine)
        idx += 1

    params.extend([limit, offset])
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags,
                   category, importance, created_at
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


async def get_categories() -> list[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT category FROM memories WHERE category IS NOT NULL ORDER BY category"
        )
        return [r["category"] for r in rows]


async def get_all_tags() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT unnest(tags) as tag, count(*) as cnt FROM memories GROUP BY tag ORDER BY cnt DESC"
        )
        return [dict(r) for r in rows]


async def get_entity_by_id(entity_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, entity_type, canonical_name, description,
                   mention_count, metadata, created_at, updated_at
            FROM entities WHERE id = $1
            """,
            UUID(entity_id),
        )
        return _row_to_dict(row) if row else None


async def get_entity_connections(entity_id: str) -> list[dict]:
    """Get direct connections for an entity (for node detail panel)."""
    pool = await get_pool()
    eid = UUID(entity_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT er.id, er.relationship_type, er.confidence,
                   CASE WHEN er.source_entity_id = $1 THEN 'outgoing' ELSE 'incoming' END AS direction,
                   CASE WHEN er.source_entity_id = $1 THEN e.name ELSE se.name END AS connected_name,
                   CASE WHEN er.source_entity_id = $1 THEN e.entity_type ELSE se.entity_type END AS connected_type,
                   CASE WHEN er.source_entity_id = $1 THEN e.id ELSE se.id END AS connected_id
            FROM entity_relations er
            LEFT JOIN entities e ON e.id = er.target_entity_id
            LEFT JOIN entities se ON se.id = er.source_entity_id
            WHERE (er.source_entity_id = $1 OR er.target_entity_id = $1)
              AND er.valid = true
            ORDER BY er.confidence DESC
            """,
            eid,
        )
        return [_row_to_dict(row) for row in rows]


async def list_entities(
    *,
    entity_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    pool = await get_pool()
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if entity_type:
        conditions.append(f"entity_type = ${idx}")
        params.append(entity_type)
        idx += 1

    params.extend([limit, offset])
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, name, entity_type, canonical_name, description,
                   mention_count, created_at
            FROM entities
            WHERE {where}
            ORDER BY mention_count DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


# ──────────────────────────────────────────────
# Stats
# ──────────────────────────────────────────────

async def get_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Single query for all scalar counts
        counts = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM memories) AS total_memories,
                (SELECT count(*) FROM conversations_raw) AS raw_conversations,
                (SELECT count(*) FROM entities) AS total_entities,
                (SELECT count(*) FROM entity_relations WHERE valid = true) AS total_relations,
                (SELECT count(*) FROM memories WHERE extraction_status = 'done') AS extraction_done,
                (SELECT count(*) FROM memories WHERE extraction_status IS NULL OR extraction_status = 'failed') AS extraction_pending
            """
        )
        by_source = await conn.fetch(
            "SELECT source_type, count(*) as cnt FROM memories GROUP BY source_type ORDER BY cnt DESC"
        )
        by_category = await conn.fetch(
            "SELECT category, count(*) as cnt FROM memories WHERE category IS NOT NULL GROUP BY category ORDER BY cnt DESC"
        )
        by_machine = await conn.fetch(
            "SELECT source_machine, count(*) as cnt FROM memories WHERE source_machine IS NOT NULL GROUP BY source_machine ORDER BY cnt DESC"
        )
        top_tags = await conn.fetch(
            "SELECT unnest(tags) as tag, count(*) as cnt FROM memories GROUP BY tag ORDER BY cnt DESC LIMIT 20"
        )

        return {
            "total_memories": counts["total_memories"],
            "raw_conversations": counts["raw_conversations"],
            "total_entities": counts["total_entities"],
            "total_relations": counts["total_relations"],
            "extraction_done": counts["extraction_done"],
            "extraction_pending": counts["extraction_pending"],
            "by_source": [dict(r) for r in by_source],
            "by_category": [dict(r) for r in by_category],
            "by_machine": [dict(r) for r in by_machine],
            "top_tags": [dict(r) for r in top_tags],
        }


async def get_scheduler_events(limit: int = 50) -> list[dict]:
    """Get recent scheduler and agent events for the dashboard."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_id, event_type, category, description, metadata, created_at
            FROM agent_events
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(row) for row in rows]


async def get_feedback_stats() -> dict:
    """Get feedback and archive statistics for the dashboard."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        feedback_total = await conn.fetchval("SELECT count(*) FROM memory_outcomes")
        feedback_positive = await conn.fetchval(
            "SELECT count(*) FROM memory_outcomes WHERE was_useful = true"
        )
        archived = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE category = '_archived'"
        )
        events_24h = await conn.fetchval(
            "SELECT count(*) FROM agent_events WHERE created_at > now() - interval '24 hours'"
        )
        return {
            "feedback_total": feedback_total,
            "feedback_positive": feedback_positive,
            "archived_memories": archived,
            "events_24h": events_24h,
        }


async def get_cross_machine_clusters(limit: int = 3) -> list[dict]:
    """Find entity clusters that span multiple machines."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                e.id as entity_id,
                e.name as entity_name,
                e.entity_type,
                array_agg(DISTINCT m.source_machine) as machines,
                count(DISTINCT m.source_machine) as machine_count,
                array_agg(DISTINCT LEFT(m.content, 300)) as memory_contents
            FROM entities e
            JOIN entity_memories em ON em.entity_id = e.id
            JOIN memories m ON m.id = em.memory_id
            WHERE m.category != '_archived'
              AND m.source_machine IS NOT NULL
              AND m.source_machine != ''
            GROUP BY e.id, e.name, e.entity_type
            HAVING count(DISTINCT m.source_machine) >= 2
            AND NOT EXISTS (
                SELECT 1 FROM memories m2
                WHERE m2.source_type = 'cross_machine_insight'
                  AND m2.metadata->>'entity_id' = e.id::text
                  AND m2.created_at > now() - interval '7 days'
            )
            ORDER BY count(DISTINCT m.source_machine) DESC, count(*) DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def get_extraction_samples(limit: int = 10) -> list[dict]:
    """Get recently extracted entities with their source memory for quality check."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                e.id as entity_id,
                e.name as entity_name,
                e.entity_type,
                e.description as entity_description,
                m.id as memory_id,
                LEFT(m.content, 500) as memory_content,
                em.confidence as link_confidence
            FROM entity_memories em
            JOIN entities e ON e.id = em.entity_id
            JOIN memories m ON m.id = em.memory_id
            WHERE e.created_at > now() - interval '7 days'
              AND NOT EXISTS (
                  SELECT 1 FROM agent_events ae
                  WHERE ae.event_type = 'extraction_validated'
                    AND ae.metadata->>'entity_id' = e.id::text
              )
            ORDER BY e.created_at DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def update_entity_confidence(entity_id: str, memory_id: str, confidence: float) -> None:
    """Update extraction confidence for an entity-memory link."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE entity_memories SET confidence = $1 WHERE entity_id = $2 AND memory_id = $3",
            confidence, UUID(entity_id), UUID(memory_id),
        )


async def store_raw_conversation(
    source_type: str,
    title: str | None,
    messages: list[dict],
    *,
    source_file: str | None = None,
    metadata: dict | None = None,
) -> str | None:
    """Store a raw conversation, skipping if a duplicate (same title+message_count) exists."""
    pool = await get_pool()
    msg_count = len(messages)
    async with pool.acquire() as conn:
        # Dedup check: same source_type + title + message_count
        existing = await conn.fetchval(
            """
            SELECT id FROM conversations_raw
            WHERE source_type = $1 AND title = $2 AND message_count = $3
            LIMIT 1
            """,
            source_type,
            title,
            msg_count,
        )
        if existing is not None:
            return None  # duplicate

        row = await conn.fetchrow(
            """
            INSERT INTO conversations_raw (source_type, source_file, title, messages, message_count, metadata)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)
            RETURNING id
            """,
            source_type,
            source_file,
            title,
            json.dumps(messages),
            msg_count,
            _jsonb(metadata),
        )
        return str(row["id"])


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("id", "source_id", "target_id", "source_entity_id", "target_entity_id",
                "memory_id", "entity_id", "relation_id", "connected_id"):
        if key in d and d[key] is not None:
            d[key] = str(d[key])
    for key in ("created_at", "updated_at", "last_accessed_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    if "metadata" in d and d["metadata"] is not None:
        if isinstance(d["metadata"], str):
            d["metadata"] = json.loads(d["metadata"])
    if "similarity" in d:
        d["similarity"] = round(float(d["similarity"]), 4)
    if "relevance" in d:
        d["relevance"] = round(float(d["relevance"]), 4)
    if "importance" in d and d["importance"] is not None:
        d["importance"] = round(float(d["importance"]), 4)
    if "stability" in d and d["stability"] is not None:
        d["stability"] = round(float(d["stability"]), 4)
    if "confidence" in d and d["confidence"] is not None:
        d["confidence"] = round(float(d["confidence"]), 4)
    if "quality_score" in d and d["quality_score"] is not None:
        d["quality_score"] = round(float(d["quality_score"]), 4)
    return d


def _jsonb(data: dict | None) -> str:
    return json.dumps(data or {})


# ──────────────────────────────────────────────
# Entity web research (Phase 3)
# ──────────────────────────────────────────────

async def get_research_candidates(
    min_mentions: int = 5,
    cooldown_days: int = 14,
    limit: int = 3,
) -> list[dict]:
    """Find entities worth researching on the web.

    Criteria: important entities (5+ mentions) with thin descriptions,
    no web research in the last N days, and no existing crawled memory about them.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.name, e.entity_type, e.canonical_name,
                   e.description, e.mention_count,
                   ARRAY_AGG(DISTINCT m.content ORDER BY m.importance DESC)
                       FILTER (WHERE m.content IS NOT NULL) AS memory_contents
            FROM entities e
            LEFT JOIN entity_memories em ON em.entity_id = e.id
            LEFT JOIN memories m ON m.id = em.memory_id AND m.category <> '_archived'
            WHERE e.mention_count >= $1
              AND e.entity_type IN ('technology', 'project', 'concept', 'organization')
              -- No web research event in last N days
              AND NOT EXISTS (
                  SELECT 1 FROM agent_events ae
                  WHERE ae.event_type = 'web_research'
                    AND ae.metadata->>'entity_id' = e.id::text
                    AND ae.created_at > NOW() - INTERVAL '1 day' * $2
              )
            GROUP BY e.id, e.name, e.entity_type, e.canonical_name,
                     e.description, e.mention_count
            ORDER BY e.mention_count DESC
            LIMIT $3
            """,
            min_mentions,
            cooldown_days,
            limit,
        )
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["memory_contents"] = d["memory_contents"] or []
            result.append(d)
        return result


# ──────────────────────────────────────────────
# Interest signals (Phase 5)
# ──────────────────────────────────────────────

async def ensure_interest_signals_table() -> None:
    """Create the interest_signals table if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS interest_signals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                topic TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                strength FLOAT DEFAULT 1.0,
                source_machine TEXT,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interest_signals_topic
            ON interest_signals (topic);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interest_signals_created
            ON interest_signals (created_at DESC);
        """)


async def record_interest_signal(
    topic: str,
    signal_type: str,
    strength: float = 1.0,
    source_machine: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Record an interest signal for a topic."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO interest_signals (topic, signal_type, strength, source_machine, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            topic, signal_type, strength, source_machine, _jsonb(metadata),
        )


async def get_hot_topics(
    decay_days: int = 30,
    limit: int = 10,
) -> list[dict]:
    """Get topics ranked by recent interest (time-decayed signal strength).

    Each signal decays exponentially: strength * exp(-age_days / decay_days).
    Returns topics with their aggregated weighted score.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                topic,
                SUM(
                    strength * EXP(-EXTRACT(EPOCH FROM (NOW() - created_at)) / (86400.0 * $1))
                ) AS score,
                COUNT(*) AS signal_count,
                MAX(created_at) AS last_signal
            FROM interest_signals
            WHERE created_at > NOW() - INTERVAL '1 day' * ($1 * 3)
            GROUP BY topic
            HAVING SUM(
                strength * EXP(-EXTRACT(EPOCH FROM (NOW() - created_at)) / (86400.0 * $1))
            ) > 0.1
            ORDER BY score DESC
            LIMIT $2
            """,
            decay_days,
            limit,
        )
        return [dict(r) for r in rows]


async def get_topic_research_status(topic: str) -> dict | None:
    """Check if a topic has been recently researched."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT MAX(created_at) AS last_researched
            FROM agent_events
            WHERE event_type = 'interest_research'
              AND metadata->>'topic' = $1
            """,
            topic,
        )
        if row and row["last_researched"]:
            return {"topic": topic, "last_researched": row["last_researched"].isoformat()}
        return None
