"""Database query functions for memories, entities, and the knowledge graph."""

import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime
from uuid import UUID

import asyncpg
import numpy as np

from nobrainr.config import settings as _cfg
from nobrainr.db.pool import get_pool
from nobrainr.events import publish

logger = logging.getLogger("nobrainr")

# Halfvec cast string for HNSW index utilization (e.g. "halfvec(1024)")
_HV = f"halfvec({_cfg.embedding_dimensions})"
# Full vector cast string for explicit typing in outer queries
_VEC = f"vector({_cfg.embedding_dimensions})"


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
    fts_context: str | None = None,
    event_ts: "datetime | None" = None,
) -> dict:
    """Insert a new memory row.

    event_ts (2026-04-20): when provided, overrides the default now() for
    created_at. The write queue worker passes the queue row's
    enqueued_at so the timeline reflects *when the agent observed the
    event*, not *when the LLM pipeline finished processing it*. Without
    this, a memory enqueued 2 days ago but dequeued today shows up as
    "today" on the dashboard — misleading for historical recall.
    """
    from nobrainr.config import settings
    from nobrainr.utils.tags import canonicalize_tags

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, summary, embedding, source_type, source_machine,
                                  source_ref, tags, category, confidence, metadata,
                                  embedding_model, fts_context, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, COALESCE($13, now()))
            RETURNING id, created_at
            """,
            content,
            summary,
            np.array(embedding, dtype=np.float32),
            source_type,
            source_machine,
            source_ref,
            canonicalize_tags(tags),
            category,
            confidence,
            _jsonb(metadata),
            settings.embedding_model,
            fts_context,
            event_ts,
        )
        result = {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}
        publish("memory_created", {"id": result["id"]})
        # Version 0 is recorded automatically by the trg_memory_version_insert trigger
        return result


async def find_similar_memories(
    embedding: list[float],
    *,
    limit: int = 5,
    threshold: float = 0.85,
    exclude_id: str | None = None,
) -> list[dict]:
    """Find memories similar to the given embedding (for dedup checks).

    Includes the embedding_model alias safeguard so dedup decisions are
    not made against memories embedded with a stale or differently-tagged
    model — that mismatch would let near-duplicates slip through ADD
    even when an UPDATE-able twin exists.
    """
    pool = await get_pool()
    vec = np.array(embedding, dtype=np.float32)
    model_aliases = list(_cfg.embedding_model_aliases or [_cfg.embedding_model])
    async with pool.acquire() as conn:
        if exclude_id:
            rows = await conn.fetch(
                f"""
                SELECT id, content, summary, tags, category,
                       1 - (embedding <=> $1) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL
                  AND (embedding_model IS NULL OR embedding_model = ANY($4::text[]))
                  AND id != $3
                ORDER BY embedding::{_HV} <=> $1::{_HV}
                LIMIT $2
                """,
                vec, limit, UUID(exclude_id), model_aliases,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT id, content, summary, tags, category,
                       1 - (embedding <=> $1) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL
                  AND (embedding_model IS NULL OR embedding_model = ANY($3::text[]))
                ORDER BY embedding::{_HV} <=> $1::{_HV}
                LIMIT $2
                """,
                vec, limit, model_aliases,
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
    include_cold: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
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
            include_cold=include_cold,
            date_from=date_from, date_to=date_to,
        )

    # Two-phase vector search: halfvec HNSW index scan for candidates → full-precision re-ranking
    # Only search memories embedded with the current model to avoid cross-model garbage

    # ANY($4::text[]) so every equivalent model label matches — see
    # settings.embedding_model_aliases.
    conditions = [
        "embedding IS NOT NULL",
        f"(embedding_model IS NULL OR embedding_model = ANY(${4}::text[]))",
    ]
    if not include_cold:
        conditions.append("tier < 3")
    params: list = [vec, threshold, limit, list(_cfg.embedding_model_aliases or [_cfg.embedding_model])]
    idx = 5

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
    # Temporal filters (v6.5, 2026-04-11) — phase 1 of the query intent
    # parser work. Agents can pass date_from/date_to today; a future LLM-
    # based parser will extract "last week" / "before Thursday" into
    # these same params. Both are inclusive bounds on created_at.
    if date_from is not None:
        conditions.append(f"created_at >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to is not None:
        conditions.append(f"created_at <= ${idx}")
        params.append(date_to)
        idx += 1

    where = " AND ".join(conditions)
    # Overfetch budget matches Anthropic Contextual Retrieval (top-150 → top-20
    # reranked): 15× with reranker, 5× without (there's no cross-encoder to
    # recover the tail so we don't want to pay for unused candidates).
    overfetch_mult = 15 if _cfg.reranker_enabled else 5
    overfetch = max(limit * overfetch_mult, 30)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            WITH candidates AS (
                SELECT *
                FROM memories
                WHERE {where}
                  AND 1 - (embedding::{_HV} <=> $1::{_HV}) >= $2
                ORDER BY embedding::{_HV} <=> $1::{_HV}
                LIMIT {overfetch}
            )
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   access_count, last_accessed_at, quality_score, embedding_model, tier, trust_score, verified_at, superseded_by, claim_kind,
                   1 - (embedding <=> $1::{_VEC}) AS similarity,
                   memory_relevance($1::{_VEC}, embedding, created_at, importance, stability, access_count, now(), quality_score) AS relevance
            FROM candidates
            ORDER BY relevance DESC
            LIMIT $3
            """,
            *params,
        )
        results = [_row_to_dict(row) for row in rows]

        # Dynamic recall thresholding: drop results below 50% of top score
        if results:
            top_sim = max(float(r.get("similarity", 0)) for r in results)
            if top_sim > 0:
                dynamic_floor = top_sim * 0.5
                results = [r for r in results if float(r.get("similarity", 0)) >= dynamic_floor]

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
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[str, list, int]:
    """Build shared WHERE filter fragment for hybrid search sub-queries.

    v6.5 (2026-04-11): extended with optional date_from/date_to to enable
    temporal-aware hybrid search. Both are inclusive bounds on created_at
    and participate in the same numbered-placeholder sequence as the
    existing filters.
    """
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
    if date_from is not None:
        conditions.append(f"created_at >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to is not None:
        conditions.append(f"created_at <= ${idx}")
        params.append(date_to)
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
    include_cold: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict]:
    """Hybrid search using Reciprocal Rank Fusion of vector + full-text results."""
    # Anthropic's Contextual Retrieval recipe: retrieve top-150 → rerank to top-20.
    # We mirror that when the reranker is enabled.
    overfetch_mult = 15 if _cfg.reranker_enabled else 5
    overfetch = max(limit * overfetch_mult, 30)
    # Inner overfetch for halfvec candidate retrieval before full-precision re-rank
    inner_overfetch = overfetch * 3
    tier_filter = "" if include_cold else " AND tier < 3"

    async with pool.acquire() as conn:
        # 1) Vector search: halfvec HNSW scan → full-precision re-rank
        vec_extra, vec_fparams, _ = _build_filter_clause(
            5, tags, category, source_type, source_machine,
            date_from=date_from, date_to=date_to,
        )
        vec_rows = await conn.fetch(
            f"""
            WITH vec_candidates AS (
                SELECT *
                FROM memories
                WHERE embedding IS NOT NULL
                  AND (embedding_model IS NULL OR embedding_model = ANY($4::text[]))
                  AND 1 - (embedding::{_HV} <=> $1::{_HV}) >= $2
                  {vec_extra}{tier_filter}
                ORDER BY embedding::{_HV} <=> $1::{_HV}
                LIMIT {inner_overfetch}
            )
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   access_count, last_accessed_at, quality_score, embedding_model, tier, trust_score, verified_at, superseded_by, claim_kind,
                   1 - (embedding <=> $1::{_VEC}) AS similarity,
                   memory_relevance($1::{_VEC}, embedding, created_at, importance, stability, access_count, now(), quality_score) AS relevance
            FROM vec_candidates
            ORDER BY relevance DESC
            LIMIT $3
            """,
            embedding, threshold, overfetch,
            list(_cfg.embedding_model_aliases or [_cfg.embedding_model]),
            *vec_fparams,
        )

        # 2) Full-text search: $1=query, $2=overfetch, filters from $3+
        # Multilingual 'simple' + unaccent: matches German/Dutch/Spanish/etc.
        # content that the old English-only index silently mis-tokenised.
        fts_extra, fts_fparams, _ = _build_filter_clause(
            3, tags, category, source_type, source_machine,
            date_from=date_from, date_to=date_to,
        )
        fts_rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   access_count, last_accessed_at, quality_score, embedding_model, tier, trust_score, verified_at, superseded_by, claim_kind,
                   ts_rank(
                       to_tsvector('simple', nb_unaccent(content || ' ' || COALESCE(search_keys, '') || ' ' || COALESCE(fts_context, ''))),
                       plainto_tsquery('simple', nb_unaccent($1))
                   ) AS fts_rank
            FROM memories
            WHERE to_tsvector('simple', nb_unaccent(content || ' ' || COALESCE(search_keys, '') || ' ' || COALESCE(fts_context, '')))
                  @@ plainto_tsquery('simple', nb_unaccent($1))
              {fts_extra}{tier_filter}
            ORDER BY fts_rank DESC
            LIMIT $2
            """,
            text_query, overfetch, *fts_fparams,
        )

        # 2b) Literal ID-token branch (2026-04-19). Hex hashes, issue/PR
        # numbers, UUID-prefixes get tokenised away by FTS and carry no
        # signal in the embedding — so commit-hash-specific queries
        # ("commit 798461a6") used to miss even when the exact memory
        # existed in the corpus. Run an extra substring match on each
        # ID token and feed it into RRF with the standard weight.
        from nobrainr.utils.id_tokens import extract_id_tokens
        id_tokens = extract_id_tokens(text_query)
        id_rows: list = []
        if id_tokens:
            # Params: $1..$N = tokens, $N+1 = limit, $N+2+ = filter params
            n = len(id_tokens)
            token_ors = " OR ".join(
                f"content ILIKE '%' || ${i + 1} || '%' "
                f"OR COALESCE(search_keys, '') ILIKE '%' || ${i + 1} || '%'"
                for i in range(n)
            )
            id_extra, id_fparams, _ = _build_filter_clause(
                n + 2, tags, category, source_type, source_machine,
                date_from=date_from, date_to=date_to,
            )
            id_sql = f"""
                SELECT id, content, summary, source_type, source_machine, tags, category,
                       confidence, metadata, created_at, updated_at, importance, stability,
                       access_count, last_accessed_at, quality_score, embedding_model, tier, trust_score, verified_at, superseded_by, claim_kind,
                       1.0::real AS literal_score
                FROM memories
                WHERE ({token_ors})
                  {id_extra}{tier_filter}
                ORDER BY importance DESC NULLS LAST, updated_at DESC
                LIMIT ${n + 1}
            """
            id_rows = await conn.fetch(
                id_sql, *id_tokens, overfetch, *id_fparams,
            )

        # 2c) Graph-aware branch (2026-04-19, HippoRAG-lite). Fuzzy-match the
        # query against entities.canonical_name via pg_trgm, then lift memories
        # linked to those entities. Cheap proxy for full Personalized PageRank
        # that captures most of the associative-memory lift without the graph
        # algorithm overhead. Only runs for queries of meaningful length
        # (short queries → too many trigram candidates) and when the graph is
        # populated. Optional via settings.graph_branch_enabled.
        graph_rows: list = []
        if (
            _cfg.graph_branch_enabled
            and text_query
            and len(text_query) >= _cfg.graph_branch_min_query_chars
        ):
            g_extra, g_fparams, _ = _build_filter_clause(
                4, tags, category, source_type, source_machine,
                date_from=date_from, date_to=date_to,
            )
            # Set trigram threshold low enough to catch variations; hub
            # dampening (idf specificity) keeps high-fanout entities from
            # dominating — generic hubs like "Python" shouldn't swamp the
            # retrieval for every query mentioning Python.
            graph_sql = f"""
                WITH matched_entities AS (
                    SELECT id, similarity(canonical_name, $1) AS sim,
                           COALESCE(specificity, 1.0) AS idf
                    FROM entities
                    WHERE canonical_name % $1
                      AND similarity(canonical_name, $1) >= $2
                    ORDER BY sim DESC
                    LIMIT $3
                )
                SELECT m.id, m.content, m.summary, m.source_type, m.source_machine,
                       m.tags, m.category, m.confidence, m.metadata,
                       m.created_at, m.updated_at, m.importance, m.stability,
                       m.access_count, m.last_accessed_at, m.quality_score,
                       m.embedding_model, m.tier, m.trust_score, m.verified_at, m.superseded_by, m.claim_kind,
                       SUM(me.sim * me.idf * COALESCE(em.confidence, 0.5)) AS graph_score
                FROM memories m
                JOIN entity_memories em ON em.memory_id = m.id
                JOIN matched_entities me ON me.id = em.entity_id
                WHERE true {g_extra}{tier_filter}
                GROUP BY m.id
                ORDER BY graph_score DESC
                LIMIT $4
            """
            try:
                graph_rows = await conn.fetch(
                    graph_sql,
                    text_query,
                    _cfg.graph_branch_trigram_threshold,
                    _cfg.graph_branch_max_entities,
                    overfetch,
                    *g_fparams,
                )
            except Exception as exc:
                # If entities schema is missing specificity (older deploys),
                # swallow — graph branch is additive, not load-bearing.
                logger.debug("graph branch skipped: %s", exc)
                graph_rows = []

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

        # Literal branch gets full RRF weight — an exact hash match is
        # AS strong a signal as a high-ranked vector or FTS hit.
        for rank, row in enumerate(id_rows, start=1):
            rid = str(row["id"])
            rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (rrf_k + rank)
            if rid not in rows_by_id:
                rows_by_id[rid] = row

        # Graph-aware branch. Dampened weight so it lifts associative
        # matches without drowning the strictly-semantic top hits.
        for rank, row in enumerate(graph_rows, start=1):
            rid = str(row["id"])
            rrf_scores[rid] = (
                rrf_scores.get(rid, 0.0)
                + _cfg.graph_branch_rrf_weight / (rrf_k + rank)
            )
            if rid not in rows_by_id:
                rows_by_id[rid] = row

        # Trust-aware tie-break: stable sort first by RRF, then bubble higher trust_score
        # to break ties at equal RRF. Demotes superseded/contradicted memories that slipped
        # through the tier filter. Per /gpt-researcher 2026-04-28 finding F6.
        def _trust_of(rid: str) -> float:
            row = rows_by_id.get(rid)
            if row is None:
                return 0.5
            try:
                ts = row["trust_score"] if "trust_score" in row.keys() else None
            except Exception:
                ts = None
            return float(ts) if ts is not None else 0.5

        all_sorted = sorted(
            rrf_scores,
            key=lambda r: (rrf_scores[r], _trust_of(r)),
            reverse=True,
        )

        # Source diversity cap: no single source_type > 50% of results.
        # Without this, chatgpt (73.6% of corpus) dominates every query
        # by sheer probability — ~7/10 results from chatgpt regardless of
        # relevance. Only applies when caller hasn't already filtered by
        # source_type, and only for limit >= 4 (too aggressive otherwise).
        if source_type is None and limit >= 4:
            max_per_source = max(1, limit // 2)
            source_counts: dict[str, int] = {}
            diverse_ids: list[str] = []
            deferred: list[str] = []
            for rid in all_sorted:
                src = str(rows_by_id[rid].get("source_type") or "unknown")
                if source_counts.get(src, 0) < max_per_source:
                    source_counts[src] = source_counts.get(src, 0) + 1
                    diverse_ids.append(rid)
                else:
                    deferred.append(rid)
                if len(diverse_ids) >= limit:
                    break
            # Fill remaining slots if diversity reduced results below limit
            if len(diverse_ids) < limit:
                diverse_ids.extend(deferred[:limit - len(diverse_ids)])
            sorted_ids = diverse_ids
        else:
            sorted_ids = all_sorted[:limit]

        results = []
        for rid in sorted_ids:
            row = rows_by_id[rid]
            d = _row_to_dict(row)
            d["rrf_score"] = rrf_scores[rid]
            results.append(d)

        # Dynamic recall thresholding: drop results below 50% of top score
        if results:
            top_sim = max(float(r.get("similarity", 0)) for r in results)
            if top_sim > 0:
                dynamic_floor = top_sim * 0.5
                results = [r for r in results if float(r.get("similarity", 0)) >= dynamic_floor]

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
                   quality_score, quality_specificity, quality_actionability, quality_self_containment,
                   tier
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


async def _set_provenance(conn, *, changed_by: str, change_type: str = "", change_reason: str = ""):
    """Set session variables for the versioning trigger to read."""
    await conn.execute("SELECT set_config('nobrainr.changed_by', $1, true)", changed_by)
    if change_type:
        await conn.execute("SELECT set_config('nobrainr.change_type', $1, true)", change_type)
    if change_reason:
        await conn.execute("SELECT set_config('nobrainr.change_reason', $1, true)", change_reason)


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
    _changed_by: str | None = None,
    _change_type: str | None = None,
    _change_reason: str | None = None,
) -> dict | None:
    from nobrainr.utils.tags import canonicalize_tags

    pool = await get_pool()
    sets = []
    params = []
    idx = 1

    if tags is not None:
        tags = canonicalize_tags(tags)

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
        async with conn.transaction():
            if _changed_by:
                await _set_provenance(
                    conn,
                    changed_by=_changed_by,
                    change_type=_change_type or "",
                    change_reason=_change_reason or "",
                )
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


# ──────────────────────────────────────────────
# Tombstones (Phase H, v6.10, 2026-04-12)
# ──────────────────────────────────────────────
# doobidoo pattern — record a content-hash on every delete so the write
# queue dedup classifier can short-circuit to NOOP when the same content
# comes back. See schema.py for the table rationale.


def _compute_content_hash(content: str) -> str:
    """Normalize + SHA256-hex. Stable across leading/trailing whitespace
    and case variations so near-duplicate re-ingestions hit the tombstone."""
    normalized = (content or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def create_tombstone(
    content: str,
    *,
    original_memory_id: str | None = None,
    reason: str = "manual_delete",
) -> dict:
    """Record a tombstone for deleted content.

    Idempotent: if a tombstone for the same content_hash already exists,
    the existing row is returned via an ON CONFLICT DO UPDATE no-op trick.
    This lets the caller not care whether the content was tombstoned once
    already (e.g. a user deleting the same memory twice).
    """
    content_hash = _compute_content_hash(content)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory_tombstones (content_hash, original_memory_id, reason)
            VALUES ($1, $2, $3)
            ON CONFLICT (content_hash) DO UPDATE
                SET created_at = memory_tombstones.created_at
            RETURNING id, content_hash, original_memory_id, reason, created_at
            """,
            content_hash,
            UUID(original_memory_id) if original_memory_id else None,
            reason,
        )
        return _row_to_dict(row)


async def is_tombstoned(content: str) -> bool:
    """True if this exact content (normalized) has a tombstone on record.

    Called by the write-queue dedup classifier before the expensive
    similarity search. Single indexed lookup on the unique content_hash
    index — cheap and bounded.
    """
    content_hash = _compute_content_hash(content)
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM memory_tombstones WHERE content_hash = $1 LIMIT 1",
            content_hash,
        )
        return exists is not None


async def get_tombstone(content: str) -> dict | None:
    """Fetch the full tombstone row by content. Useful for dashboards and
    post-mortem when you want to know WHY a write classified as NOOP."""
    content_hash = _compute_content_hash(content)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, content_hash, original_memory_id, reason, created_at
            FROM memory_tombstones WHERE content_hash = $1
            """,
            content_hash,
        )
        return _row_to_dict(row) if row else None


async def delete_memory(
    memory_id: str,
    *,
    _changed_by: str | None = None,
    _change_type: str | None = None,
    _change_reason: str | None = None,
) -> bool:
    """Delete a memory AND record a tombstone so its content can't be
    silently re-ingested by the write queue.

    Phase H (v6.10, 2026-04-12): the tombstone is written INSIDE the
    same transaction as the DELETE. Either both happen or neither, so
    the DB never sits in a "memory deleted but re-ingestible" state.
    ON CONFLICT on content_hash is a no-op that preserves the earliest
    tombstone creation timestamp.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if _changed_by:
                await _set_provenance(
                    conn,
                    changed_by=_changed_by,
                    change_type=_change_type or "manual_delete",
                    change_reason=_change_reason or "",
                )

            # Fetch content BEFORE the DELETE so we can tombstone it.
            content_row = await conn.fetchrow(
                "SELECT content FROM memories WHERE id = $1",
                UUID(memory_id),
            )
            if content_row is None:
                return False
            content = content_row["content"]

            result = await conn.execute(
                "DELETE FROM memories WHERE id = $1",
                UUID(memory_id),
            )
            deleted = result == "DELETE 1"

            if deleted:
                # Tombstone inside the same transaction for atomicity.
                content_hash = _compute_content_hash(content)
                await conn.execute(
                    """
                    INSERT INTO memory_tombstones (content_hash, original_memory_id, reason)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (content_hash) DO UPDATE
                        SET created_at = memory_tombstones.created_at
                    """,
                    content_hash,
                    UUID(memory_id),
                    _change_reason or _change_type or "manual_delete",
                )

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
    """Restore a memory to a specific version.

    The BEFORE UPDATE trigger automatically snapshots the current state
    before the restore overwrites it.
    """
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

        # Re-embed the restored content
        new_embedding = None
        try:
            from nobrainr.embeddings.ollama import embed_text
            new_embedding = await embed_text(snapshot["content"])
        except Exception:
            pass

        # Apply the snapshot — trigger records the pre-restore state automatically
        async with conn.transaction():
            await _set_provenance(
                conn,
                changed_by="manual",
                change_type="restore",
                change_reason=f"Restored to version {version}",
            )
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
        conditions.append(
            f"to_tsvector('simple', nb_unaccent(content)) "
            f"@@ plainto_tsquery('simple', nb_unaccent(${idx}))"
        )
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

# Batch size for online maintenance UPDATEs. At 2000 rows per batch each
# transaction holds row locks for tens of milliseconds rather than tens of
# seconds, so concurrent access_count bumps from memory_get almost never
# deadlock with us. See recompute_importance() / decay_stability() below.
_MAINTENANCE_BATCH_SIZE = 2000


async def _run_with_deadlock_retry(coro_factory, *, retries: int = 4, label: str = "update"):
    """Await ``coro_factory()`` and retry on Postgres deadlock.

    asyncpg's DeadlockDetectedError is transient — Postgres picked one
    transaction to abort so the other could finish. The cheap, correct
    response is to wait a little and re-run. Jittered backoff keeps
    parallel batches from re-colliding.
    """
    for attempt in range(retries):
        try:
            return await coro_factory()
        except asyncpg.exceptions.DeadlockDetectedError:
            if attempt == retries - 1:
                logger.warning("%s: deadlock persisted after %d retries", label, retries)
                raise
            delay = 0.1 * (2 ** attempt) + random.uniform(0, 0.1)
            logger.info("%s: deadlock on attempt %d, retrying in %.2fs", label, attempt + 1, delay)
            await asyncio.sleep(delay)


async def recompute_importance() -> int:
    """Recompute importance using graph-structural signals + quality score.

    v6.6 (2026-04-12, Phase B G1): added graph proximity signal — how many
    "hot entities" (those linked to any memory accessed in the last 7 days)
    are linked to this memory. Graphiti-inspired rerank-by-graph-distance.

    v6.12 (2026-04-12, Phase J): added typed-edge downrank penalty —
    memories whose entities appear in the "stale" position of a supersede/
    deprecate edge get their importance pushed down by up to 0.15. Uses
    the two directions of the relationship_type vocabulary already
    produced by the LLM extractor:

      - ``source_entity_id`` of ('replaced_by', 'superseded_by',
        'deprecated_in', 'deprecated_since') — the entity was replaced
      - ``target_entity_id`` of ('replaces', 'supersedes',
        'aims_to_replace', 'can_replace_with') — the entity was replaced
        by something else

    A memory heavily connected to entities in either stale position is
    likely carrying outdated information. This complements (doesn't
    conflict with) the Mem0-style SUPERSEDE write path, which physically
    replaces the memory on the way in; this downrank catches the
    residual signal for memories that survived the classifier.

    Formula (positive terms sum to 1.0, penalty subtracts after):
      + 30% entity connectivity
      + 30% quality score
      + 20% confidence
      + 20% graph proximity (v6.6, Phase B G1)
      − up to 15% outdated-edge penalty (v6.12, Phase J)
      clamped to [0, 1] via GREATEST(0.0, LEAST(1.0, ...))

    v6.13 (2026-04-22): split into a read-only compute phase and a
    batched apply phase. The old single table-wide UPDATE held row
    locks for tens of seconds and deadlocked against the access-count
    bump that fires on every memory_get. Now we materialise new values
    first (no locks on memories) and apply them in 2000-row chunks with
    deadlock-retry per chunk.
    """
    pool = await get_pool()

    # Phase 1: read-only compute. SELECT doesn't take row-exclusive locks
    # on memories, so it coexists cleanly with live access_count bumps.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH hot_entities AS (
                -- Entities linked to any memory accessed in the last 7 days.
                SELECT DISTINCT em.entity_id
                FROM entity_memories em
                JOIN memories m2 ON m2.id = em.memory_id
                WHERE m2.last_accessed_at > NOW() - INTERVAL '7 days'
            ),
            memory_hot_counts AS (
                SELECT em.memory_id,
                       count(DISTINCT em.entity_id) AS hot_entity_count
                FROM entity_memories em
                WHERE em.entity_id IN (SELECT entity_id FROM hot_entities)
                GROUP BY em.memory_id
            ),
            stale_entities AS (
                SELECT source_entity_id AS entity_id
                FROM entity_relations
                WHERE relationship_type IN (
                    'replaced_by', 'superseded_by',
                    'deprecated_in', 'deprecated_since'
                )
                UNION
                SELECT target_entity_id AS entity_id
                FROM entity_relations
                WHERE relationship_type IN (
                    'replaces', 'supersedes',
                    'aims_to_replace', 'can_replace_with'
                )
            ),
            memory_stale_counts AS (
                SELECT em.memory_id,
                       count(DISTINCT em.entity_id) AS stale_entity_count
                FROM entity_memories em
                WHERE em.entity_id IN (SELECT entity_id FROM stale_entities)
                GROUP BY em.memory_id
            )
            SELECT m.id,
                   GREATEST(0.0, LEAST(1.0,
                       (0.3 * LEAST(1.0, COALESCE((
                           SELECT count(*)::real / 10.0
                           FROM entity_memories em WHERE em.memory_id = m.id
                       ), 0.0)))
                     + (0.3 * COALESCE(m.quality_score, 0.5))
                     + (0.2 * COALESCE(m.confidence, 0.7))
                     + (0.2 * LEAST(1.0, COALESCE((
                           SELECT hot_entity_count::real / 10.0
                           FROM memory_hot_counts mhc WHERE mhc.memory_id = m.id
                       ), 0.0)))
                     - (0.15 * LEAST(1.0, COALESCE((
                           SELECT stale_entity_count::real / 5.0
                           FROM memory_stale_counts msc WHERE msc.memory_id = m.id
                       ), 0.0)))
                   ))::real AS new_importance,
                   m.importance AS old_importance
            FROM memories m
            WHERE m.embedding IS NOT NULL
            """
        )

    # Keep only rows whose value actually changes. No-op UPDATEs still take
    # row locks in Postgres, so pre-filtering cuts the working set hard.
    changed = [
        (r["id"], float(r["new_importance"]))
        for r in rows
        if r["old_importance"] is None
        or abs(float(r["old_importance"]) - float(r["new_importance"])) > 1e-6
    ]
    if not changed:
        return 0

    # Phase 2: apply in batches, each its own tiny transaction with retry.
    total = 0
    for i in range(0, len(changed), _MAINTENANCE_BATCH_SIZE):
        batch = changed[i : i + _MAINTENANCE_BATCH_SIZE]
        ids = [row[0] for row in batch]
        imps = [row[1] for row in batch]

        async def _apply_batch():
            pool_ = await get_pool()
            async with pool_.acquire() as conn:
                async with conn.transaction():
                    return await conn.execute(
                        """
                        UPDATE memories m
                        SET importance = data.new_importance
                        FROM unnest($1::uuid[], $2::real[]) AS data(id, new_importance)
                        WHERE m.id = data.id
                          AND m.importance IS DISTINCT FROM data.new_importance
                        """,
                        ids,
                        imps,
                    )

        result = await _run_with_deadlock_retry(
            _apply_batch, label=f"recompute_importance batch {i // _MAINTENANCE_BATCH_SIZE}"
        )
        total += int(result.split()[-1]) if result else 0

    return total


async def decay_stability() -> int:
    """Decay stability for memories not accessed in 7+ days.

    v6.13 (2026-04-22): same treatment as recompute_importance — pick
    target ids in a lock-free SELECT, then apply in 2000-row batches so
    concurrent access-count bumps don't deadlock us.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id
            FROM memories
            WHERE (last_accessed_at IS NULL AND created_at < now() - interval '7 days')
               OR (last_accessed_at < now() - interval '7 days')
            """
        )

    ids_all = [r["id"] for r in rows]
    if not ids_all:
        return 0

    total = 0
    for i in range(0, len(ids_all), _MAINTENANCE_BATCH_SIZE):
        chunk = ids_all[i : i + _MAINTENANCE_BATCH_SIZE]

        async def _apply_batch():
            pool_ = await get_pool()
            async with pool_.acquire() as conn:
                async with conn.transaction():
                    return await conn.execute(
                        """
                        UPDATE memories
                        SET stability = GREATEST(0.1, stability * 0.95)
                        WHERE id = ANY($1::uuid[])
                        """,
                        chunk,
                    )

        result = await _run_with_deadlock_retry(
            _apply_batch, label=f"decay_stability batch {i // _MAINTENANCE_BATCH_SIZE}"
        )
        total += int(result.split()[-1]) if result else 0

    return total


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


async def analyze_tables() -> dict:
    """Run ANALYZE on core tables + apply retention to audit_log and memory_versions.

    Returns counts of rows pruned so the scheduler can log them.

    Retention policy:
      - audit_log: 7 days. Aggressive because UPDATE-heavy scheduler jobs
        generate ~100K audit rows/day.
      - memory_versions: keep the 5 most-recent versions per memory_id.
        Without this trimming, the table grows unbounded — observed 1496 versions
        for a single memory after the 2026-03-31 to 2026-04-07 backfill.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("ANALYZE memories")
        await conn.execute("ANALYZE entities")
        await conn.execute("ANALYZE entity_memories")
        await conn.execute("ANALYZE entity_relations")

        # Audit log retention — 7 days
        audit_result = await conn.execute(
            "DELETE FROM audit_log WHERE created_at < NOW() - INTERVAL '7 days'"
        )
        audit_pruned = int(audit_result.split()[-1]) if audit_result else 0

        # memory_versions retention — keep last 5 per memory_id
        versions_result = await conn.execute(
            """
            DELETE FROM memory_versions
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY memory_id ORDER BY version DESC
                    ) AS rn
                    FROM memory_versions
                ) t
                WHERE rn > 5
            )
            """
        )
        versions_pruned = int(versions_result.split()[-1]) if versions_result else 0

        if audit_pruned or versions_pruned:
            import logging
            logging.getLogger("nobrainr").info(
                "Retention: pruned %d audit_log rows (>7d) + %d memory_versions rows (keep-5)",
                audit_pruned, versions_pruned,
            )

        return {
            "audit_log_pruned": audit_pruned,
            "memory_versions_pruned": versions_pruned,
        }


async def store_memory_outcome(
    memory_id: str,
    was_useful: bool,
    *,
    context: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    query_trace_id: str | None = None,
    query_text: str | None = None,
    result_rank: int | None = None,
) -> dict:
    """Record feedback on whether a memory search result was useful.

    Optional trace fields (v6, 2026-04-11) let the caller link a feedback
    row back to the specific search that surfaced the memory — required
    for computing MRR/NDCG in a later phase. All three must be present
    together to carry meaning, but any combination is accepted so the
    dashboard thumbs-down still works without query context.
    """
    pool = await get_pool()
    # Normalise + guard
    trace_uuid: UUID | None = None
    if query_trace_id:
        try:
            trace_uuid = UUID(query_trace_id)
        except (ValueError, AttributeError):
            trace_uuid = None
    if result_rank is not None and result_rank < 1:
        # Only 1-indexed ranks make sense; drop anything else as "unknown".
        result_rank = None
    # Trim freakishly long queries — full query goes in the agent's memory,
    # not ours. 500 chars is enough for later bucketing/diagnostics.
    trimmed_query = query_text[:500] if query_text else None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory_outcomes (
                memory_id, was_useful, context, agent_id, session_id,
                query_trace_id, query_text, result_rank
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, created_at
            """,
            UUID(memory_id), was_useful, context, agent_id, session_id,
            trace_uuid, trimmed_query, result_rank,
        )
        publish(
            "feedback_added",
            {
                "memory_id": memory_id,
                "was_useful": was_useful,
                "query_trace_id": str(trace_uuid) if trace_uuid else None,
                "result_rank": result_rank,
            },
        )
        return {
            "id": str(row["id"]),
            "created_at": row["created_at"].isoformat(),
            "traced": trace_uuid is not None,
        }


async def integrate_feedback_scores() -> int:
    """Adjust importance based on memory_outcomes feedback.

    Only trust feedback that shows variance. The pre-2026-04-09 version would
    UP-rank any memory with 2+ positive votes — which meant our 88K
    auto-generated "was_useful=true" records silently inflated importance
    on every memory the agent touched, without real signal.

    New rule: a memory must have >=5 feedback events AND at least one
    negative vote before the importance adjustment fires. This way the
    only memories that move are those the agent has actually disagreed
    with — which is where the signal actually is.
    """
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
                       COUNT(*) FILTER (WHERE was_useful) ::real / COUNT(*) AS positive_ratio,
                       COUNT(*) FILTER (WHERE NOT was_useful) AS neg_count
                FROM memory_outcomes
                GROUP BY memory_id
                HAVING COUNT(*) >= 5
                   AND COUNT(*) FILTER (WHERE NOT was_useful) >= 1
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
    *,
    seed_sample: int = 200,
    neighbours_per_seed: int = 5,
) -> list[dict]:
    """Find memory pairs with high cosine similarity that haven't been
    consolidation-checked.

    Pre-2026-05-02 implementation did a full N×N self-join with two
    embedding columns (HNSW can't accelerate column-vs-column compares),
    which collapsed the DB into a 4-worker parallel scan eating 96% CPU
    each for 18+ minutes once memories crossed ~50k rows. Same operator,
    quadratically worse with every new memory.

    HNSW-friendly rewrite: pick a small random sample of seed memories,
    use the index to find each seed's K nearest neighbours (ANN, sub-ms
    per probe), then filter by threshold and de-dup. Cost is
    O(seed_sample × log N × K), bounded and fast regardless of table size.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH seeds AS (
                SELECT id, embedding, content
                  FROM memories
                 WHERE embedding IS NOT NULL AND tier < 3
              ORDER BY random()
                 LIMIT $3
            ),
            pairs AS (
                SELECT s.id AS id_a, s.content AS content_a,
                       n.id AS id_b, n.content AS content_b,
                       1 - (s.embedding <=> n.embedding) AS similarity
                  FROM seeds s
                  CROSS JOIN LATERAL (
                      SELECT m.id, m.content, m.embedding
                        FROM memories m
                       WHERE m.embedding IS NOT NULL
                         AND m.id <> s.id
                    ORDER BY m.embedding <=> s.embedding
                       LIMIT $4
                  ) n
                 WHERE 1 - (s.embedding <=> n.embedding) > $1
            )
            SELECT id_a, content_a, id_b, content_b, similarity
              FROM (
                SELECT DISTINCT ON (LEAST(id_a::text, id_b::text), GREATEST(id_a::text, id_b::text))
                       CASE WHEN id_a::text < id_b::text THEN id_a ELSE id_b END AS id_a,
                       CASE WHEN id_a::text < id_b::text THEN content_a ELSE content_b END AS content_a,
                       CASE WHEN id_a::text < id_b::text THEN id_b ELSE id_a END AS id_b,
                       CASE WHEN id_a::text < id_b::text THEN content_b ELSE content_a END AS content_b,
                       similarity
                  FROM pairs
              ) ordered
             WHERE NOT EXISTS (
                 SELECT 1 FROM agent_events
                  WHERE event_type = 'consolidation_checked'
                    AND metadata->>'id_a' = id_a::text
                    AND metadata->>'id_b' = id_b::text
             )
          ORDER BY similarity DESC
             LIMIT $2
            """,
            threshold, limit, seed_sample, neighbours_per_seed,
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
        async with conn.transaction():
            await _set_provenance(
                conn,
                changed_by="scheduler:memory_decay",
                change_type="decay_archive",
                change_reason="Low-value, never-accessed, >30 days old",
            )
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
    mid = memory_id if isinstance(memory_id, UUID) else UUID(memory_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET extraction_status = $1 WHERE id = $2",
            status, mid,
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

def _canonical_entity_name(name: str) -> str:
    """Normalise an entity name for deduplication.

    Lower-cases, strips whitespace, and *strips diacritics* so "Düsseldorf"
    and "Dusseldorf" hash to the same canonical entity. Otherwise the same
    place gets two graph nodes — one tagged from a German source and one
    from an English source — and neither benefits from the other's mentions.
    """
    import unicodedata
    s = name.strip().lower()
    # NFKD decomposition → drop combining marks → ASCII-fold what we can.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Common German ligatures that NFKD doesn't fold
    s = s.replace("ß", "ss").replace("œ", "oe").replace("æ", "ae")
    return s


async def find_or_create_entity(
    name: str,
    entity_type: str,
    *,
    description: str | None = None,
    embedding: list[float] | None = None,
) -> str:
    """Find entity by canonical name or create it. Returns entity ID."""
    canonical = _canonical_entity_name(name)
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
            INSERT INTO entities (name, entity_type, canonical_name, description, embedding, embedding_model)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (canonical_name, entity_type) DO UPDATE
                SET mention_count = entities.mention_count + 1
            RETURNING id
            """,
            name, entity_type, canonical, description, vec, _cfg.embedding_model,
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
    """Semantic search on entity embeddings.

    Applies the same embedding_model alias safeguard as search_memories so
    label drift can never silently hide most of the entity graph.
    """
    pool = await get_pool()
    vec = np.array(embedding, dtype=np.float32)
    model_aliases = list(_cfg.embedding_model_aliases or [_cfg.embedding_model])
    conditions = [
        "embedding IS NOT NULL",
        "(embedding_model IS NULL OR embedding_model = ANY($3::text[]))",
    ]
    params: list = [vec, limit, model_aliases]
    idx = 4

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
            ORDER BY embedding::{_HV} <=> $1::{_HV}
            LIMIT $2
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


async def get_entity_graph(entity_name: str, depth: int = 2, max_nodes: int = 200) -> dict:
    """Get entity and its connections via recursive CTE traversal."""
    canonical = _canonical_entity_name(entity_name)
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
            LIMIT $3
            """,
            start_id, depth, max_nodes * 3,
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

        result = {"nodes": list(nodes_map.values()), "edges": edges}
        if len(edges) >= max_nodes * 3:
            result["truncated"] = True
            result["hint"] = f"Graph truncated at {max_nodes * 3} edges. Use max_nodes or reduce depth."
        return result


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


async def search_facts(
    embedding: list[float],
    *,
    limit: int = 10,
    threshold: float = 0.3,
    text_query: str | None = None,
    date_asof: datetime | None = None,
) -> list[dict]:
    """Search atomic facts by embedding similarity, optionally with text matching.

    Applies the same embedding_model safeguard as memories to prevent
    cross-model garbage during re-embed migrations. Uses the alias list so
    label drift (cpu/gpu, :0.6b, :latest) never hides valid facts.

    Phase K (v6.15, 2026-04-12): adds bi-temporal filtering on the
    ``valid_from`` / ``valid_to`` columns.
      - ``date_asof=None`` (default): return only CURRENTLY valid facts
        (``valid_to IS NULL``). This is a behavior change from pre-K
        where superseded facts would still surface — they now stay in
        the table for audit but are hidden from normal searches.
      - ``date_asof=<datetime>``: point-in-time query — return facts
        that were valid AT that timestamp (``valid_from <= date_asof``
        AND (``valid_to IS NULL`` OR ``valid_to > date_asof``)). This
        is the Zep / Graphiti "what did we believe on date X" pattern.
    """
    pool = await get_pool()
    model_aliases = list(_cfg.embedding_model_aliases or [_cfg.embedding_model])

    # Bi-temporal WHERE fragment + its param list. Built once and
    # injected into each of the three query variants so the vector-only,
    # hybrid, and hybrid-fallback paths all share the same temporal
    # semantics.
    if date_asof is None:
        temporal_clause = " AND f.valid_to IS NULL"
        temporal_params: list = []
    else:
        temporal_clause = (
            " AND f.valid_from <= ${N1}"
            " AND (f.valid_to IS NULL OR f.valid_to > ${N2})"
        )
        temporal_params = [date_asof, date_asof]

    async with pool.acquire() as conn:
        # Check if table exists and has data
        has_facts = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM memory_facts WHERE embedding IS NOT NULL LIMIT 1)"
        )
        if not has_facts:
            return []

        if text_query:
            # Hybrid: vector + text match, multilingual via unaccent + trigram
            # Param slots: $1=embedding, $2=limit, $3=text, $4=model_aliases,
            # then $5, $6 for temporal bounds when date_asof is set.
            hybrid_temporal = temporal_clause.replace("{N1}", "5").replace("{N2}", "6")
            rows = await conn.fetch(
                f"""
                SELECT f.id, f.content, f.memory_id, f.quality_score, f.created_at,
                       f.valid_from, f.valid_to,
                       1 - (f.embedding::halfvec(1024) <=> $1::halfvec(1024)) as similarity
                FROM memory_facts f
                WHERE f.embedding IS NOT NULL
                  AND (f.embedding_model IS NULL OR f.embedding_model = ANY($4::text[]))
                  AND LENGTH(f.content) > 30
                  AND nb_unaccent(f.content) ILIKE '%' || nb_unaccent($3) || '%'
                  {hybrid_temporal}
                ORDER BY f.embedding::halfvec(1024) <=> $1::halfvec(1024)
                LIMIT $2
                """,
                embedding, limit * 2, text_query, model_aliases, *temporal_params,
            )
            if len(rows) < limit:
                # Fall back to pure vector if text match too few.
                # Param slots: $1=embedding, $2=limit, $3=model_aliases,
                # then $4, $5 for temporal bounds.
                vec_fallback_temporal = temporal_clause.replace("{N1}", "4").replace("{N2}", "5")
                rows = await conn.fetch(
                    f"""
                    SELECT f.id, f.content, f.memory_id, f.quality_score, f.created_at,
                           f.valid_from, f.valid_to,
                           1 - (f.embedding::halfvec(1024) <=> $1::halfvec(1024)) as similarity
                    FROM memory_facts f
                    WHERE f.embedding IS NOT NULL
                      AND (f.embedding_model IS NULL OR f.embedding_model = ANY($3::text[]))
                      AND LENGTH(f.content) > 30
                      {vec_fallback_temporal}
                    ORDER BY f.embedding::halfvec(1024) <=> $1::halfvec(1024)
                    LIMIT $2
                    """,
                    embedding, limit, model_aliases, *temporal_params,
                )
        else:
            # Pure vector path.
            # Param slots: $1=embedding, $2=limit, $3=model_aliases,
            # then $4, $5 for temporal bounds.
            vec_temporal = temporal_clause.replace("{N1}", "4").replace("{N2}", "5")
            rows = await conn.fetch(
                f"""
                SELECT f.id, f.content, f.memory_id, f.quality_score, f.created_at,
                       f.valid_from, f.valid_to,
                       1 - (f.embedding::halfvec(1024) <=> $1::halfvec(1024)) as similarity
                FROM memory_facts f
                WHERE f.embedding IS NOT NULL
                  AND (f.embedding_model IS NULL OR f.embedding_model = ANY($3::text[]))
                  AND LENGTH(f.content) > 30
                  {vec_temporal}
                ORDER BY f.embedding::halfvec(1024) <=> $1::halfvec(1024)
                LIMIT $2
                """,
                embedding, limit, model_aliases, *temporal_params,
            )
        results = [
            {**_row_to_dict(r), "similarity": float(r["similarity"])}
            for r in rows
            if float(r["similarity"]) >= threshold
        ]
        # Dynamic recall thresholding: drop results below 50% of top score
        if results:
            top_sim = max(r["similarity"] for r in results)
            if top_sim > 0:
                dynamic_floor = top_sim * 0.5
                results = [r for r in results if r["similarity"] >= dynamic_floor]
        return results


async def search_facts_prioritized(
    embedding: list[float],
    *,
    limit: int = 10,
    threshold: float = 0.3,
    text_query: str | None = None,
    date_asof: datetime | None = None,
    skip_canonical: bool = False,
) -> list[dict]:
    """Priority cascade search: check canonical (tier=1) facts first.

    Phase K implementation of the 3-tiered Graph RAG pattern:
      1. Check tier=1 (canonical) facts first — if found, STOP and return
      2. If no canonical match, fall back to normal vector search

    This prevents vector-based retrieval from overriding verified facts.
    The canonical tier acts as a "ground truth" layer that always wins.

    Args:
        embedding: Query embedding vector.
        limit: Max results to return.
        threshold: Minimum similarity score.
        text_query: Optional text for hybrid search.
        date_asof: Point-in-time filter (None = current facts only).
        skip_canonical: If True, skip tier=1 check (for debugging).

    Returns:
        List of facts, with tier=1 results first if any matched.
    """
    pool = await get_pool()
    model_aliases = list(_cfg.embedding_model_aliases or [_cfg.embedding_model])

    # Bi-temporal WHERE clause
    if date_asof is None:
        temporal_clause = " AND f.valid_to IS NULL"
        temporal_params: list = []
    else:
        temporal_clause = (
            " AND f.valid_from <= ${N1}"
            " AND (f.valid_to IS NULL OR f.valid_to > ${N2})"
        )
        temporal_params = [date_asof, date_asof]

    async with pool.acquire() as conn:
        # Step 1: Check canonical (tier=1) facts first — this is the priority cascade
        if not skip_canonical:
            tier1_temporal = temporal_clause.replace("{N1}", "4").replace("{N2}", "5")
            canonical_rows = await conn.fetch(
                f"""
                SELECT f.id, f.content, f.memory_id, f.quality_score, f.created_at,
                       f.valid_from, f.valid_to, f.tier, f.verified_at, f.verified_by,
                       1 - (f.embedding::halfvec(1024) <=> $1::halfvec(1024)) as similarity
                FROM memory_facts f
                WHERE f.embedding IS NOT NULL
                  AND f.tier = 1
                  AND (f.embedding_model IS NULL OR f.embedding_model = ANY($3::text[]))
                  AND LENGTH(f.content) > 30
                  {tier1_temporal}
                ORDER BY f.embedding::halfvec(1024) <=> $1::halfvec(1024)
                LIMIT $2
                """,
                embedding, limit, model_aliases, *temporal_params,
            )

            canonical_results = [
                {**_row_to_dict(r), "similarity": float(r["similarity"]), "priority": "canonical"}
                for r in canonical_rows
                if float(r["similarity"]) >= threshold
            ]

            # If canonical facts found above threshold, return them immediately
            if canonical_results:
                return canonical_results

        # Step 2: No canonical match — fall back to normal vector search (all tiers)
        return await search_facts(
            embedding,
            limit=limit,
            threshold=threshold,
            text_query=text_query,
            date_asof=date_asof,
        )


async def promote_fact(
    fact_id: str,
    *,
    verified_by: str = "user",
    tier: int = 1,
) -> dict | None:
    """Promote a fact to canonical (tier=1) status.

    Phase K: marks a fact as verified/canonical so it takes priority
    in search_facts_prioritized queries. Once promoted, this fact will
    be returned before any vector-based results.

    Args:
        fact_id: UUID of the fact to promote.
        verified_by: Who verified this fact (user ID, agent name, etc.).
        tier: Target tier (1=canonical, 2=historical). Default 1.

    Returns:
        Updated fact dict, or None if fact not found.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE memory_facts
            SET tier = $2, verified_at = now(), verified_by = $3
            WHERE id = $1
            RETURNING *
            """,
            UUID(fact_id), tier, verified_by,
        )
        return _row_to_dict(row) if row else None


async def demote_fact(
    fact_id: str,
) -> dict | None:
    """Demote a fact back to derived (tier=3) status.

    Reverses a promote_fact call — clears verified_at/verified_by
    and sets tier back to default.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE memory_facts
            SET tier = 3, verified_at = NULL, verified_by = NULL
            WHERE id = $1
            RETURNING *
            """,
            UUID(fact_id),
        )
        return _row_to_dict(row) if row else None


async def supersede_fact(
    fact_id: str,
    *,
    new_content: str | None = None,
    new_memory_id: str | None = None,
    new_embedding: list[float] | None = None,
    reason: str | None = None,
) -> dict | None:
    """Supersede a fact — set its valid_to=now() and optionally insert a
    replacement with valid_from=now().

    Phase K (v6.15): the bi-temporal equivalent of DELETE-and-INSERT for
    facts. The old fact stays in the table (audit trail, point-in-time
    queries) but is hidden from current-state searches (which filter to
    ``valid_to IS NULL``).

    If ``new_content`` is provided, a replacement fact is INSERTed in
    the same transaction so the supersession is atomic. If only a
    ``reason`` is given without a replacement, the old fact is just
    marked as no-longer-valid (a "retraction" rather than a replacement).

    Args:
        fact_id: UUID of the fact to supersede.
        new_content: If provided, insert a replacement fact with this
            content and valid_from=now().
        new_memory_id: Parent memory UUID for the replacement. If None,
            inherits from the superseded fact.
        new_embedding: Pre-computed embedding for the replacement. If
            None, the replacement will have NULL embedding until the
            re-embed scheduler picks it up.
        reason: Free-form reason, stored in a comment log (not yet
            persisted — future schema addition).

    Returns:
        ``{"superseded_id": str, "new_id": str | None, "reason": str | None}``
        or None if the fact_id was not found / already superseded.
    """
    try:
        old_uuid = UUID(fact_id)
    except (ValueError, AttributeError):
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Fetch the old fact (for memory_id inheritance + existence check).
            # Lock the row so a concurrent supersede doesn't race.
            old = await conn.fetchrow(
                """
                SELECT id, memory_id, content
                FROM memory_facts
                WHERE id = $1
                  AND valid_to IS NULL
                FOR UPDATE
                """,
                old_uuid,
            )
            if old is None:
                return None

            # Mark the old fact as no-longer-valid.
            await conn.execute(
                """
                UPDATE memory_facts
                SET valid_to = now()
                WHERE id = $1
                """,
                old_uuid,
            )

            new_id = None
            if new_content is not None:
                # Insert a replacement fact. valid_from defaults to now(),
                # valid_to stays NULL (currently valid).
                parent_memory_id = (
                    UUID(new_memory_id)
                    if new_memory_id
                    else old["memory_id"]
                )
                embedding_param = None
                if new_embedding is not None:
                    embedding_param = np.array(new_embedding, dtype=np.float32)
                row = await conn.fetchrow(
                    """
                    INSERT INTO memory_facts (memory_id, content, embedding,
                                              embedding_model, created_at)
                    VALUES ($1, $2, $3, $4, now())
                    RETURNING id
                    """,
                    parent_memory_id,
                    new_content,
                    embedding_param,
                    _cfg.embedding_model,
                )
                new_id = str(row["id"])

    return {
        "superseded_id": str(old_uuid),
        "new_id": new_id,
        "reason": reason,
    }


async def get_memory_facts(memory_id: str) -> list[dict]:
    """Get all facts extracted from a specific memory."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, quality_score, created_at
            FROM memory_facts
            WHERE memory_id = $1 AND LENGTH(content) > 30
            ORDER BY created_at
            """,
            UUID(memory_id),
        )
        return [_row_to_dict(r) for r in rows]


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

        # Strategy 3: Fuzzy name matching — strip non-alphanum chars, compare
        # Catches: "IFC Open Shell" vs "IfcOpenShell" vs "ifc-open-shell"
        remaining2 = limit - len(name_dupes) - len(embed_dupes)
        fuzzy_dupes = []
        if remaining2 > 0:
            fuzzy_dupes = await conn.fetch(
                """
                SELECT a.id AS id_a, a.name AS name_a, a.entity_type AS type_a,
                       a.mention_count AS mentions_a,
                       (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = a.id) AS mem_count_a,
                       b.id AS id_b, b.name AS name_b, b.entity_type AS type_b,
                       b.mention_count AS mentions_b,
                       (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = b.id) AS mem_count_b,
                       0.95::float AS similarity
                FROM entities a
                JOIN entities b ON a.id < b.id
                WHERE a.canonical_name != b.canonical_name
                  AND LOWER(REGEXP_REPLACE(a.name, '[^a-zA-Z0-9]', '', 'g'))
                    = LOWER(REGEXP_REPLACE(b.name, '[^a-zA-Z0-9]', '', 'g'))
                  AND LENGTH(REGEXP_REPLACE(a.name, '[^a-zA-Z0-9]', '', 'g')) >= 4
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_events
                      WHERE event_type = 'entity_merge_checked'
                        AND metadata->>'id_a' = a.id::text
                        AND metadata->>'id_b' = b.id::text
                  )
                ORDER BY a.mention_count + b.mention_count DESC
                LIMIT $1
                """,
                remaining2,
            )

        return [_row_to_dict(r) for r in list(name_dupes) + list(embed_dupes) + list(fuzzy_dupes)]


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
    """Delete extraction-noise entities — SURGICAL version.

    Prior versions of this function deleted anything with <=1 memory link after
    72h. On a research-heavy corpus that meant 46% of all entities (~19K of 42K)
    including valuable niche content (academic paper titles, German urban-planning
    terms, species names, rare-but-real organizations). Permanent loss.

    This version restricts to entity_types that are almost always extraction noise
    when single-mentioned:
      command, config, file, container, commit, composable, function

    These types catch:
      CLI tokens      ("apt-get", "bash", "build:prod", "__bytes__")
      config keys     ("settings", "CX42", "DEFAULT_REPLACEMENTS")
      filenames       ("README", "requirements.txt", ".gitignore" — but bridges excluded)
      container names (throwaway docker container names)
      git hashes      (full SHA extracted as "entity")
      Vue composable function names
      Generic function names

    Guards (all required):
      1. entity_type IN noise_types           — only types listed above
      2. created_at < NOW() - min_age_hours  — gives new entities a grace period
      3. mention_count <= 1                   — multi-mentioned entities are real
      4. bridge_score IS NULL                 — cross-community bridges stay,
                                                even if in a "noise" type
                                                (e.g. README, Dockerfile, curl)
      5. name not in pending-memory content   — don't remove what re-extraction
                                                would rediscover

    Valuable types (concept, technology, project, person, organization, location,
    error, package, database, component, event, feature, service) are NEVER touched
    by this job, no matter how few memory links they have.
    """
    NOISE_TYPES = (
        'command', 'config', 'file', 'container',
        'commit', 'composable', 'function',
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Log what will be pruned BEFORE deleting (lightweight — just names)
        pruned_names = await conn.fetch(
            """
            SELECT e.name, e.entity_type
            FROM entities e
            WHERE e.entity_type = ANY($2::text[])
              AND e.created_at < NOW() - make_interval(hours => $1)
              AND (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = e.id) <= 1
              AND (e.metadata->>'bridge_score' IS NULL)
            LIMIT 500
            """,
            min_age_hours,
            list(NOISE_TYPES),
        )
        if pruned_names:
            import logging
            _log = logging.getLogger("nobrainr")
            names = [f"{r['name']}({r['entity_type']})" for r in pruned_names[:20]]
            _log.info(
                "Entity pruning (surgical): deleting up to %d entities (sample: %s)",
                len(pruned_names), ", ".join(names),
            )

        # Surgical prune: same guards as the sample query above. All 5 guards
        # are in the SQL so we can't accidentally widen the scope.
        result = await conn.execute(
            """
            DELETE FROM entities
            WHERE id IN (
                SELECT e.id
                FROM entities e
                WHERE e.entity_type = ANY($2::text[])
                  AND e.created_at < NOW() - make_interval(hours => $1)
                  AND (SELECT COUNT(*) FROM entity_memories em WHERE em.entity_id = e.id) <= 1
                  AND (e.metadata->>'bridge_score' IS NULL)
                  AND NOT EXISTS (
                      SELECT 1 FROM memories m
                      WHERE (m.extraction_status IS NULL OR m.extraction_status = 'pending')
                        AND m.content ILIKE '%' || e.name || '%'
                      LIMIT 1
                  )
            )
            """,
            min_age_hours,
            list(NOISE_TYPES),
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


async def compute_entity_specificity() -> dict:
    """Compute IDF-like specificity scores for all entities.

    specificity = ln(total_memories / max(memory_links, 1))
    High specificity = rare, niche entity (IfcOpenShell, pgRouting) = valuable
    Low specificity = generic hub (Python, Docker) = less valuable for linking

    Also ensures the 'specificity' column exists (self-healing schema).

    Delta-only update path: we compute the new values in a CTE and only
    UPDATE rows whose specificity changes by more than 0.0001 (well below
    the resolution that matters for IDF ranking). The previous version
    rewrote every row on every run, which blew up audit_log by ~38K writes
    per 4-hour cycle (~230K/day). On steady-state runs this now typically
    writes only the handful of entities that gained/lost memory links since
    the previous cycle.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Self-healing: add column if it doesn't exist
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE entities ADD COLUMN IF NOT EXISTS specificity float;
            EXCEPTION WHEN duplicate_column THEN NULL;
            END $$;
        """)

        total_memories = await conn.fetchval("SELECT count(*) FROM memories")
        if total_memories == 0:
            return {"updated": 0, "total_memories_base": 0}

        result = await conn.execute(
            """
            WITH mention_counts AS (
                SELECT entity_id, count(*) AS n
                FROM entity_memories
                GROUP BY entity_id
            ),
            new_values AS (
                SELECT e.id,
                       LN($1::float / GREATEST(COALESCE(mc.n, 0), 1)) AS new_spec
                FROM entities e
                LEFT JOIN mention_counts mc ON mc.entity_id = e.id
            )
            UPDATE entities e
            SET specificity = nv.new_spec
            FROM new_values nv
            WHERE e.id = nv.id
              AND (e.specificity IS NULL
                   OR abs(e.specificity - nv.new_spec) > 0.05)
            """,
            total_memories,
        )
        updated = int(result.split()[-1]) if result else 0

        # Create index for specificity-aware queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_entities_specificity
            ON entities (specificity) WHERE specificity IS NOT NULL
        """)

        return {"updated": updated, "total_memories_base": total_memories}


async def find_bridge_entities(min_communities: int = 2, limit: int = 100) -> list[dict]:
    """Find entities that bridge multiple communities in the knowledge graph.

    Bridge entities connect different topic clusters — they are the most
    valuable nodes for cross-domain search and knowledge discovery.
    Requires community_detection to have run first (community_id set on entities).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.name, e.entity_type, e.community_id,
                   COUNT(DISTINCT other.community_id) AS communities_bridged,
                   ARRAY_AGG(DISTINCT other.community_id ORDER BY other.community_id)
                       FILTER (WHERE other.community_id IS NOT NULL) AS community_ids
            FROM entities e
            JOIN entity_relations er
                ON er.source_entity_id = e.id OR er.target_entity_id = e.id
            JOIN entities other
                ON other.id = CASE
                    WHEN er.source_entity_id = e.id THEN er.target_entity_id
                    ELSE er.source_entity_id
                END
            WHERE e.community_id IS NOT NULL
              AND other.community_id IS NOT NULL
              AND other.community_id != e.community_id
            GROUP BY e.id, e.name, e.entity_type, e.community_id
            HAVING COUNT(DISTINCT other.community_id) >= $1
            ORDER BY COUNT(DISTINCT other.community_id) DESC
            LIMIT $2
            """,
            min_communities,
            limit,
        )

        bridges = [_row_to_dict(r) for r in rows]

        # Store bridge scores in entity metadata
        if bridges:
            for b in bridges:
                await conn.execute(
                    """
                    UPDATE entities SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        'bridge_score', $2::int,
                        'bridge_communities', $3::int[]
                    )
                    WHERE id = $1
                    """,
                    UUID(b["id"]),
                    b["communities_bridged"],
                    b["community_ids"],
                )

        return bridges


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
                   category, importance, created_at, metadata
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


async def get_timeline_write_queue(
    *,
    limit: int = 50,
    category: str | None = None,
    source_machine: str | None = None,
) -> list[dict]:
    """Pending/failed items from memory_write_queue, shaped like memories for the timeline."""
    pool = await get_pool()
    conditions = ["status IN ('pending', 'failed')"]
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

    params.append(limit)
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags,
                   category, 0.0::real AS importance, enqueued_at AS created_at,
                   status AS queue_status, attempts, max_attempts, error_message
            FROM memory_write_queue
            WHERE {where}
            ORDER BY enqueued_at DESC
            LIMIT ${idx}
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
        by_embedding_model = await conn.fetch(
            """SELECT COALESCE(embedding_model, 'unknown') AS model, count(*) AS cnt
               FROM memories GROUP BY embedding_model ORDER BY cnt DESC"""
        )
        chunk_stats = await conn.fetchrow(
            """SELECT
                 count(DISTINCT metadata->>'document_id') FILTER (WHERE metadata->>'document_id' IS NOT NULL) AS documents,
                 count(*) FILTER (WHERE metadata->>'chunk_index' IS NOT NULL) AS total_chunks
               FROM memories"""
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
            "by_embedding_model": [dict(r) for r in by_embedding_model],
            "chunked_documents": chunk_stats["documents"],
            "total_chunks": chunk_stats["total_chunks"],
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
    """Get feedback and archive statistics for the dashboard.

    v6 (2026-04-11): adds trace-aware fields when query_trace_id / result_rank
    are populated on the row. Old rows (trace_id IS NULL) are excluded from
    the rank averages but still count toward total/positive.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        feedback_total = await conn.fetchval("SELECT count(*) FROM memory_outcomes")
        feedback_positive = await conn.fetchval(
            "SELECT count(*) FROM memory_outcomes WHERE was_useful = true"
        )
        feedback_traced = await conn.fetchval(
            "SELECT count(*) FROM memory_outcomes WHERE query_trace_id IS NOT NULL"
        )
        feedback_with_rank = await conn.fetchval(
            "SELECT count(*) FROM memory_outcomes WHERE result_rank IS NOT NULL"
        )
        # Average rank on which positive vs negative feedback landed — a
        # proxy for search quality. Good search: useful results cluster at
        # low rank (1-3), useless results trail higher. Divergence matters
        # more than the absolute number.
        rank_row = await conn.fetchrow(
            """
            SELECT
                AVG(result_rank) FILTER (WHERE was_useful = true)::float  AS avg_useful_rank,
                AVG(result_rank) FILTER (WHERE was_useful = false)::float AS avg_useless_rank,
                COUNT(*) FILTER (WHERE was_useful = true  AND result_rank IS NOT NULL) AS useful_ranked,
                COUNT(*) FILTER (WHERE was_useful = false AND result_rank IS NOT NULL) AS useless_ranked
            FROM memory_outcomes
            WHERE result_rank IS NOT NULL
            """
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
            "feedback_traced": feedback_traced,
            "feedback_with_rank": feedback_with_rank,
            "avg_useful_rank": rank_row["avg_useful_rank"] if rank_row else None,
            "avg_useless_rank": rank_row["avg_useless_rank"] if rank_row else None,
            "useful_ranked_count": rank_row["useful_ranked"] if rank_row else 0,
            "useless_ranked_count": rank_row["useless_ranked"] if rank_row else 0,
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

async def expand_chunk_context(
    results: list[dict],
    window: int = 1,
) -> list[dict]:
    """Expand search results with adjacent chunks for context continuity.

    For each result that is a chunk (has document_id + chunk_index in metadata),
    fetch *window* chunks before and after it. Adjacent chunks are nested under
    a ``chunk_context`` key on the original result dict.

    Non-chunk results are returned unchanged.
    """
    if window < 1 or not results:
        return results

    # Collect (document_id, chunk_indices_needed) for all chunk results
    needed: dict[str, set[int]] = {}  # document_id -> set of indices
    chunk_results: list[dict] = []

    for r in results:
        meta = r.get("metadata") or {}
        doc_id = meta.get("document_id")
        idx = meta.get("chunk_index")
        total = meta.get("chunk_total", 0)
        if doc_id is not None and idx is not None:
            chunk_results.append(r)
            for offset in range(-window, window + 1):
                neighbor = idx + offset
                if 0 <= neighbor < total and neighbor != idx:
                    needed.setdefault(doc_id, set()).add(neighbor)

    if not needed:
        return results

    # Batch-fetch all needed adjacent chunks
    pool = await get_pool()
    async with pool.acquire() as conn:
        fetched: dict[str, dict[int, dict]] = {}  # doc_id -> {chunk_index -> row}
        for doc_id, indices in needed.items():
            rows = await conn.fetch(
                """
                SELECT id, content, summary, source_type, source_machine, tags, category,
                       confidence, metadata, created_at, updated_at
                FROM memories
                WHERE metadata->>'document_id' = $1
                  AND (metadata->>'chunk_index')::int = ANY($2::int[])
                ORDER BY (metadata->>'chunk_index')::int
                """,
                doc_id, list(indices),
            )
            fetched[doc_id] = {}
            for row in rows:
                d = _row_to_dict(row)
                ci = (d.get("metadata") or {}).get("chunk_index")
                if ci is not None:
                    fetched[doc_id][ci] = d

    # Attach context to chunk results
    for r in chunk_results:
        meta = r.get("metadata") or {}
        doc_id = meta["document_id"]
        idx = meta["chunk_index"]
        context = []
        for offset in range(-window, window + 1):
            neighbor = idx + offset
            if neighbor == idx:
                continue
            chunk_data = fetched.get(doc_id, {}).get(neighbor)
            if chunk_data:
                context.append(chunk_data)
        if context:
            r["chunk_context"] = sorted(context, key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))

    return results


def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("id", "source_id", "target_id", "source_entity_id", "target_entity_id",
                "memory_id", "entity_id", "relation_id", "connected_id", "superseded_by"):
        if key in d and d[key] is not None:
            d[key] = str(d[key])
    for key in ("created_at", "updated_at", "last_accessed_at", "expires_at", "verified_at"):
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
# Procedural memory (Phase C G4, 2026-04-12, v6.8)
# ──────────────────────────────────────────────
# Letta + LangGraph "procedural memory" pattern — agent-writable rules
# and instructions. Retrieved by SCOPE (not similarity) and applied at
# session start or on demand. See schema.py for the table definition
# and the rationale behind keeping this separate from the memories table.


_PROCEDURAL_VALID_SCOPES = {"global", "agent", "project", "session"}


async def store_procedural_memory(
    content: str,
    *,
    title: str | None = None,
    scope: str = "global",
    agent_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    priority: int = 50,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    expires_at: datetime | None = None,
) -> dict:
    """Store a procedural memory (rule/instruction). See schema.py."""
    if scope not in _PROCEDURAL_VALID_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_PROCEDURAL_VALID_SCOPES)}, got {scope!r}"
        )
    if scope == "agent" and not agent_id:
        raise ValueError("agent_id is required when scope='agent'")
    if scope == "project" and not project_id:
        raise ValueError("project_id is required when scope='project'")
    if scope == "session" and not session_id:
        raise ValueError("session_id is required when scope='session'")
    if not (0 <= priority <= 100):
        raise ValueError(f"priority must be in [0, 100], got {priority}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO procedural_memories (
                content, title, scope,
                agent_id, project_id, session_id,
                priority, tags, metadata, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
            RETURNING id, content, title, scope,
                      agent_id, project_id, session_id,
                      priority, active, tags, metadata,
                      created_at, updated_at, expires_at
            """,
            content, title, scope,
            agent_id, project_id, session_id,
            priority, tags or [], _jsonb(metadata), expires_at,
        )
        return _row_to_dict(row)


async def get_procedural_memories(
    *,
    scope: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    include_expired: bool = False,
    include_inactive: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Retrieve procedural memories, ordered by priority DESC.

    Scope merging semantics:
      - If ``agent_id`` is passed, return (global rules) + (agent rules
        for that specific agent). This is the common "give me the rules
        that apply to me" query.
      - If ``project_id`` is passed, return (global) + (project rules
        for that specific project).
      - If ``session_id`` is passed, return (global) + (session rules).
      - If ``scope`` is passed explicitly, filter strictly to that scope.
      - If nothing is passed, return every active rule.

    Expired and inactive rules are filtered out by default. Pass
    ``include_expired=True`` or ``include_inactive=True`` to see them
    (dashboards, audit).
    """
    if scope is not None and scope not in _PROCEDURAL_VALID_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_PROCEDURAL_VALID_SCOPES)}, got {scope!r}"
        )

    conditions = []
    params = []
    idx = 1

    if not include_inactive:
        conditions.append("active = true")
    if not include_expired:
        conditions.append("(expires_at IS NULL OR expires_at > now())")

    # Scope merging: an explicit scope filter takes precedence over the
    # "global + specific" merge. The merge only activates when no explicit
    # scope is passed AND an id is passed.
    if scope is not None:
        conditions.append(f"scope = ${idx}")
        params.append(scope)
        idx += 1
    else:
        merge_clauses = []
        if agent_id is not None:
            merge_clauses.append(
                f"(scope = 'agent' AND agent_id = ${idx})"
            )
            params.append(agent_id)
            idx += 1
        if project_id is not None:
            merge_clauses.append(
                f"(scope = 'project' AND project_id = ${idx})"
            )
            params.append(project_id)
            idx += 1
        if session_id is not None:
            merge_clauses.append(
                f"(scope = 'session' AND session_id = ${idx})"
            )
            params.append(session_id)
            idx += 1
        if merge_clauses:
            # Global rules always apply, plus whichever specific id matches
            scope_clause = "(scope = 'global' OR " + " OR ".join(merge_clauses) + ")"
            conditions.append(scope_clause)

    where = " AND ".join(conditions) if conditions else "true"
    params.append(limit)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, title, scope,
                   agent_id, project_id, session_id,
                   priority, active, tags, metadata,
                   created_at, updated_at, expires_at
            FROM procedural_memories
            WHERE {where}
            ORDER BY priority DESC, created_at DESC
            LIMIT ${idx}
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


async def delete_procedural_memory(memory_id: str, *, hard: bool = False) -> bool:
    """Deactivate (soft delete) or hard-delete a procedural memory.

    Default is soft — sets ``active = false`` so the rule leaves an
    audit trail. Pass ``hard=True`` to actually remove the row
    (dashboard cleanup, tests).
    """
    # Validate UUID BEFORE touching the pool. Garbage input must not
    # trigger a connection attempt — failing closed means tests and
    # malformed caller input both return False cheaply.
    try:
        mem_uuid = UUID(memory_id)
    except (ValueError, AttributeError):
        return False

    pool = await get_pool()
    async with pool.acquire() as conn:
        if hard:
            result = await conn.execute(
                "DELETE FROM procedural_memories WHERE id = $1",
                mem_uuid,
            )
        else:
            result = await conn.execute(
                "UPDATE procedural_memories SET active = false, updated_at = now() WHERE id = $1",
                mem_uuid,
            )
        return int(result.split()[-1]) > 0 if result else False


# ──────────────────────────────────────────────
# Related memories (Phase Q, v6.16, 2026-04-12)
# ──────────────────────────────────────────────
# Graphiti-inspired "graph expansion on retrieval". When a caller does
# memory_search, optionally surface memories that share entities with
# each result via a single batched query — one SQL round-trip for all
# result IDs, window-function-ranked by importance per source so the
# top-N per result is computed entirely server-side.


async def get_related_memories_batch(
    memory_ids: list[str],
    *,
    limit_per_memory: int = 3,
) -> dict[str, list[dict]]:
    """For each memory_id in ``memory_ids``, return the top-N memories
    that share at least one entity via ``entity_memories``.

    Single batched query — one DB round-trip regardless of
    ``len(memory_ids)``. Uses a window function
    (``ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY importance DESC)``)
    so the top-N-per-source is computed entirely server-side.

    The returned dict maps each input memory_id (as a string) to a list
    of related memories (possibly empty). Self-references are filtered
    out (a memory cannot be related to itself).

    Callers like the MCP ``memory_search`` tool invoke this after the
    main search + rerank pipeline to attach a ``related_memories`` field
    per result — the Graphiti "graph expansion on retrieval" pattern
    at effectively zero extra cost for reasonable limit values.
    """
    if not memory_ids:
        return {}

    # Normalize input to UUID objects + silently drop garbage
    uuids: list[UUID] = []
    string_keys: list[str] = []
    for mid in memory_ids:
        try:
            uuids.append(UUID(mid))
            string_keys.append(str(mid))
        except (ValueError, AttributeError):
            continue

    if not uuids:
        return {}

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH source_entities AS (
                SELECT em.memory_id AS source_id, em.entity_id
                FROM entity_memories em
                WHERE em.memory_id = ANY($1::uuid[])
            ),
            related_with_counts AS (
                -- Dedupe (source_id, related_id) pairs via GROUP BY.
                -- Without this, a memory that shares MULTIPLE entities
                -- with a source would appear in the join multiple times
                -- and get multiple ranks — a real bug observed in the
                -- 2026-04-12 live dry-run.
                -- The count itself is also the best ranking signal:
                -- more shared entities = more "related".
                SELECT
                    se.source_id,
                    m.id AS related_id,
                    m.content,
                    m.summary,
                    m.category,
                    m.tags,
                    m.importance,
                    m.created_at,
                    count(*) AS shared_entity_count
                FROM source_entities se
                JOIN entity_memories em2 ON em2.entity_id = se.entity_id
                JOIN memories m ON m.id = em2.memory_id
                WHERE m.id != se.source_id
                  AND m.embedding IS NOT NULL
                GROUP BY
                    se.source_id, m.id, m.content, m.summary, m.category,
                    m.tags, m.importance, m.created_at
            ),
            related_ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_id
                        ORDER BY shared_entity_count DESC,
                                 importance DESC,
                                 created_at DESC
                    ) AS rank
                FROM related_with_counts
            )
            SELECT source_id, related_id, content, summary, category,
                   tags, importance, created_at, shared_entity_count
            FROM related_ranked
            WHERE rank <= $2
            ORDER BY source_id, rank
            """,
            uuids,
            limit_per_memory,
        )

    # Group by source_id in Python. Every input key lands in the result
    # dict (with an empty list if no rows matched) so callers don't have
    # to do None-vs-missing branching.
    result: dict[str, list[dict]] = {k: [] for k in string_keys}
    for row in rows:
        source_key = str(row["source_id"])
        if source_key not in result:
            result[source_key] = []
        related = {
            "id": str(row["related_id"]),
            "content": row["content"],
            "summary": row["summary"],
            "category": row["category"],
            "tags": row["tags"],
            "importance": round(float(row["importance"]), 4) if row["importance"] is not None else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "shared_entity_count": int(row["shared_entity_count"]),
        }
        result[source_key].append(related)
    return result


# ──────────────────────────────────────────────
# User profile layers (Phase M, v6.14, 2026-04-12)
# ──────────────────────────────────────────────
# Supermemory-inspired dual-layer user profile: "static" (high-importance
# stable facts about the user / their setup) + "recent" (what they've
# been doing in the last N days). Agents call the MCP wrapper
# memory_get_user_profile(...) once at session start to get a compact
# ~1k-token context block, avoiding ad-hoc memory_search calls to
# reconstruct "who is the user" on every session.


async def get_user_profile_layers(
    *,
    source_machine: str | None = None,
    static_limit: int = 20,
    recent_limit: int = 15,
    recent_window_days: int = 7,
    static_importance_floor: float = 0.75,
) -> dict:
    """Return the two-layer user profile: static facts + recent activity.

    Static = high-importance memories (importance >= ``static_importance_floor``).
    These encode "who the user is, what they prefer, what they're working
    on at the architectural level" — the memories that a feedback ranker
    has surfaced as durable knowledge.

    Recent = memories touched (accessed or created) in the last
    ``recent_window_days`` days. This is the "what they've been doing
    this week" layer that complements the static snapshot.

    Both layers are optionally filtered to a specific ``source_machine``
    so agents can scope the profile to the machine they're running on
    instead of getting cross-machine noise.

    Returns ``{"static": [...], "recent": [...]}``. Each list contains
    memory dicts (id, content, summary, tags, category, importance, etc.).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        static_rows = await conn.fetch(
            """
            SELECT id, content, summary, category, tags, importance,
                   stability, confidence, source_type, source_machine,
                   created_at, updated_at
            FROM memories
            WHERE embedding IS NOT NULL
              AND importance >= $1
              AND ($2::text IS NULL OR source_machine = $2)
            ORDER BY importance DESC, stability DESC, created_at DESC
            LIMIT $3
            """,
            static_importance_floor,
            source_machine,
            static_limit,
        )
        recent_rows = await conn.fetch(
            """
            SELECT id, content, summary, category, tags, importance,
                   source_type, source_machine, created_at, updated_at,
                   last_accessed_at
            FROM memories
            WHERE embedding IS NOT NULL
              AND (
                  last_accessed_at > NOW() - (INTERVAL '1 day' * $1)
                  OR created_at > NOW() - (INTERVAL '1 day' * $1)
              )
              AND ($2::text IS NULL OR source_machine = $2)
            ORDER BY GREATEST(
                COALESCE(last_accessed_at, created_at),
                created_at
            ) DESC, importance DESC
            LIMIT $3
            """,
            recent_window_days,
            source_machine,
            recent_limit,
        )
    return {
        "static": [_row_to_dict(r) for r in static_rows],
        "recent": [_row_to_dict(r) for r in recent_rows],
    }


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


# ──────────────────────────────────────────────
# Memory tiering
# ──────────────────────────────────────────────

async def set_memory_tier(memory_id: str, tier: int) -> dict | None:
    """Manually set a memory's tier (0=pinned, 1=hot, 2=standard, 3=cold)."""
    if tier not in (0, 1, 2, 3):
        raise ValueError(f"Invalid tier {tier}, must be 0-3")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE memories SET tier = $1 WHERE id = $2 RETURNING id, tier",
            tier, UUID(memory_id),
        )
        if row:
            return {"id": str(row["id"]), "tier": row["tier"]}
        return None


async def auto_tier_memories() -> dict:
    """Auto-assign tiers based on importance, access patterns, and quality.

    Rules (applied in priority order):
    - Tier 0 (pinned): importance >= 0.9 AND access_count >= 10
    - Tier 1 (hot): accessed in last 7 days OR quality_score >= 0.8
    - Tier 3 (cold): (not accessed in 30+ days AND importance < 0.3 AND access_count < 3)
                     OR quality_score < 0.35 (LLM-assessed low quality, memory age > 7d)
    - Tier 2 (standard): everything else

    Never demotes manually-pinned tier 0 memories (those set via set_memory_tier).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Promote to tier 0: high importance + high access (but don't demote manual pins)
        r0 = await conn.execute(
            """
            UPDATE memories SET tier = 0
            WHERE tier > 0
              AND importance >= 0.9
              AND access_count >= 10
            """,
        )

        # Promote to tier 1: recently accessed or high quality
        r1 = await conn.execute(
            """
            UPDATE memories SET tier = 1
            WHERE tier > 1
              AND (
                  last_accessed_at > now() - interval '7 days'
                  OR quality_score >= 0.8
              )
            """,
        )

        # Demote to tier 3: cold memories — low engagement OR low quality
        r3 = await conn.execute(
            """
            UPDATE memories SET tier = 3
            WHERE tier = 2
              AND created_at < now() - interval '7 days'
              AND (
                  -- Low engagement: barely accessed, low importance, not recent
                  (
                      importance < 0.3
                      AND access_count < 3
                      AND (last_accessed_at IS NULL OR last_accessed_at < now() - interval '30 days')
                      AND created_at < now() - interval '30 days'
                  )
                  OR
                  -- Low quality score (LLM-assessed): regardless of access pattern
                  (
                      quality_score IS NOT NULL
                      AND quality_score < 0.35
                  )
              )
            """,
        )

        # Recover from cold: if a cold memory gets accessed, bump back to 2
        r_recover = await conn.execute(
            """
            UPDATE memories SET tier = 2
            WHERE tier = 3
              AND last_accessed_at > now() - interval '7 days'
            """,
        )

        counts = {
            "promoted_to_0": _affected(r0),
            "promoted_to_1": _affected(r1),
            "demoted_to_3": _affected(r3),
            "recovered_from_cold": _affected(r_recover),
        }
        return counts


async def get_unlinked_cooccurrences(
    min_shared: int = 3,
    limit: int = 50,
    max_memory_links: int = 300,
) -> list[dict]:
    """Find entity pairs co-occurring in min_shared+ memories with no relationship.

    Filters out hub entities (>max_memory_links memories) to avoid noise like
    "Python relates_to Docker" that drowns real signal.

    Returns rows with: entity_a_id, entity_a_name, entity_a_type,
                       entity_b_id, entity_b_name, entity_b_type,
                       shared_count, sample_memory_ids, sample_contents
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH non_hub_entities AS (
                SELECT entity_id
                FROM entity_memories
                GROUP BY entity_id
                HAVING count(*) <= $3
            ),
            cooccurrences AS (
                SELECT
                    em1.entity_id AS a_id,
                    em2.entity_id AS b_id,
                    count(DISTINCT em1.memory_id) AS shared_count,
                    array_agg(DISTINCT em1.memory_id ORDER BY em1.memory_id) AS memory_ids
                FROM entity_memories em1
                JOIN entity_memories em2
                    ON em1.memory_id = em2.memory_id
                   AND em1.entity_id < em2.entity_id
                WHERE em1.entity_id IN (SELECT entity_id FROM non_hub_entities)
                  AND em2.entity_id IN (SELECT entity_id FROM non_hub_entities)
                GROUP BY em1.entity_id, em2.entity_id
                HAVING count(DISTINCT em1.memory_id) >= $1
            ),
            unlinked AS (
                SELECT c.*
                FROM cooccurrences c
                LEFT JOIN entity_relations er
                    ON (er.source_entity_id = c.a_id AND er.target_entity_id = c.b_id)
                    OR (er.source_entity_id = c.b_id AND er.target_entity_id = c.a_id)
                WHERE er.id IS NULL
                ORDER BY c.shared_count DESC
                LIMIT $2
            )
            SELECT
                u.a_id, ea.name AS a_name, ea.entity_type AS a_type,
                u.b_id, eb.name AS b_name, eb.entity_type AS b_type,
                u.shared_count,
                u.memory_ids[1:3] AS sample_memory_ids
            FROM unlinked u
            JOIN entities ea ON ea.id = u.a_id
            JOIN entities eb ON eb.id = u.b_id
            """,
            min_shared,
            limit,
            max_memory_links,
        )

        # Fetch sample content for LLM context (up to 3 memories per pair)
        results = []
        for row in rows:
            sample_ids = row["sample_memory_ids"]
            contents = []
            if sample_ids:
                mem_rows = await conn.fetch(
                    """
                    SELECT id, LEFT(content, 500) AS content
                    FROM memories
                    WHERE id = ANY($1)
                    """,
                    sample_ids,
                )
                contents = [r["content"] for r in mem_rows]
            results.append({
                "entity_a_id": str(row["a_id"]),
                "entity_a_name": row["a_name"],
                "entity_a_type": row["a_type"],
                "entity_b_id": str(row["b_id"]),
                "entity_b_name": row["b_name"],
                "entity_b_type": row["b_type"],
                "shared_count": row["shared_count"],
                "sample_contents": contents,
            })
        return results


async def get_nearby_entities_for_memory(
    embedding: list[float],
    *,
    limit: int = 15,
    min_mentions: int = 2,
) -> list[dict]:
    """Find entities semantically close to a memory embedding.

    Used to inject neighborhood context into the extraction prompt.
    Returns entities with: id, name, entity_type, description, similarity.
    """
    pool = await get_pool()
    vec = np.array(embedding, dtype=np.float32)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, name, entity_type, description,
                   1 - (embedding <=> $1) AS similarity
            FROM entities
            WHERE embedding IS NOT NULL
              AND mention_count >= $3
            ORDER BY embedding::{_HV} <=> $1::{_HV}
            LIMIT $2
            """,
            vec,
            limit,
            min_mentions,
        )
        return [_row_to_dict(row) for row in rows]


async def get_tier_stats() -> list[dict]:
    """Get memory counts by tier."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(tier, 2) AS tier, count(*) AS count
            FROM memories
            GROUP BY COALESCE(tier, 2)
            ORDER BY tier
            """,
        )
        return [{"tier": row["tier"], "count": row["count"]} for row in rows]


def _affected(result: str) -> int:
    """Extract affected row count from asyncpg execute result string."""
    # asyncpg returns e.g. "UPDATE 42"
    parts = result.split()
    return int(parts[-1]) if parts else 0


async def get_extraction_pending_count() -> int:
    """Fast count of memories awaiting entity extraction."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM memories WHERE extraction_status IS NULL OR extraction_status = 'failed'"
        )
