"""Database schema initialization and migrations."""

from nobrainr.config import settings

SCHEMA_SQL = f"""
-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;

-- Memories: the core knowledge entries
CREATE TABLE IF NOT EXISTS memories (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    content         text NOT NULL,
    summary         text,
    embedding       vector({settings.embedding_dimensions}),
    source_type     text NOT NULL DEFAULT 'manual',
    source_machine  text,
    source_ref      text,
    tags            text[] DEFAULT '{{}}',
    category        text,
    confidence      real DEFAULT 1.0,
    metadata        jsonb DEFAULT '{{}}',
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

-- HNSW index for fast approximate nearest neighbor search
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- GIN index for tag queries
CREATE INDEX IF NOT EXISTS idx_memories_tags
    ON memories USING gin (tags);

-- Index for category filtering
CREATE INDEX IF NOT EXISTS idx_memories_category
    ON memories (category);

-- Index for source filtering
CREATE INDEX IF NOT EXISTS idx_memories_source_type
    ON memories (source_type);

-- Full-text search on content
CREATE INDEX IF NOT EXISTS idx_memories_content_fts
    ON memories USING gin (to_tsvector('english', content));

-- Raw conversation archives
CREATE TABLE IF NOT EXISTS conversations_raw (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    source_type     text NOT NULL,
    source_file     text,
    title           text,
    messages        jsonb NOT NULL,
    message_count   int,
    imported_at     timestamptz DEFAULT now(),
    metadata        jsonb DEFAULT '{{}}'
);

-- Memory relations (links between memories)
CREATE TABLE IF NOT EXISTS memory_relations (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    source_id       uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id       uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation_type   text NOT NULL DEFAULT 'related',
    strength        real DEFAULT 1.0,
    created_at      timestamptz DEFAULT now(),
    UNIQUE (source_id, target_id, relation_type)
);

-- Updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
CREATE TRIGGER trg_memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
"""


async def init_schema(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
