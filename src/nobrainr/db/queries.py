"""Database query functions for memories."""

from uuid import UUID

import asyncpg
from pgvector.asyncpg import register_vector

from nobrainr.db.pool import get_pool


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
            VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9, $10::jsonb)
            RETURNING id, created_at
            """,
            content,
            summary,
            str(embedding),
            source_type,
            source_machine,
            source_ref,
            tags or [],
            category,
            confidence,
            _jsonb(metadata),
        )
        return {"id": str(row["id"]), "created_at": row["created_at"].isoformat()}


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
    conditions = ["1=1"]
    params: list = [str(embedding), limit]
    idx = 3

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

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE {where}
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            *params,
        )
        return [
            _row_to_dict(row)
            for row in rows
            if row["similarity"] >= threshold
        ]


async def get_memory(memory_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, content, summary, source_type, source_machine, source_ref,
                   tags, category, confidence, metadata, created_at, updated_at
            FROM memories WHERE id = $1
            """,
            UUID(memory_id),
        )
        return _row_to_dict(row) if row else None


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
        sets.append(f"embedding = ${idx}::vector")
        params.append(str(embedding))
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
        return _row_to_dict(row) if row else None


async def query_memories(
    *,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    text_query: str | None = None,
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

    params.extend([limit, offset])
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
        return [_row_to_dict(row) for row in rows]


async def get_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM memories")
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
        raw_convos = await conn.fetchval("SELECT count(*) FROM conversations_raw")

        return {
            "total_memories": total,
            "raw_conversations": raw_convos,
            "by_source": [dict(r) for r in by_source],
            "by_category": [dict(r) for r in by_category],
            "by_machine": [dict(r) for r in by_machine],
            "top_tags": [dict(r) for r in top_tags],
        }


async def store_raw_conversation(
    source_type: str,
    title: str | None,
    messages: list[dict],
    *,
    source_file: str | None = None,
    metadata: dict | None = None,
) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        import json
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
            len(messages),
            _jsonb(metadata),
        )
        return str(row["id"])


def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("id", "source_id", "target_id"):
        if key in d and d[key] is not None:
            d[key] = str(d[key])
    for key in ("created_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    if "metadata" in d and d["metadata"] is not None:
        import json
        if isinstance(d["metadata"], str):
            d["metadata"] = json.loads(d["metadata"])
    if "similarity" in d:
        d["similarity"] = round(float(d["similarity"]), 4)
    return d


def _jsonb(data: dict | None) -> str:
    import json
    return json.dumps(data or {})
