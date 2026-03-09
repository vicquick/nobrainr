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
    tags            text[] DEFAULT '{{}}'::text[],
    category        text,
    confidence      real DEFAULT 1.0,
    metadata        jsonb DEFAULT '{{}}'::jsonb,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now(),
    -- v2 columns
    last_accessed_at timestamptz,
    access_count    integer DEFAULT 0,
    stability       real DEFAULT 1.0,
    importance      real DEFAULT 0.0,
    extraction_status text
);

-- Add v2 columns to existing tables (safe for re-runs)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at timestamptz;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS access_count integer DEFAULT 0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS stability real DEFAULT 1.0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS importance real DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS extraction_status text;

-- v3: Quality scoring columns
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_score real;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_specificity smallint;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_actionability smallint;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_self_containment smallint;

-- HNSW index for fast approximate nearest neighbor search
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

-- GIN index for tag queries
CREATE INDEX IF NOT EXISTS idx_memories_tags
    ON memories USING gin (tags);

-- Index for category filtering
CREATE INDEX IF NOT EXISTS idx_memories_category
    ON memories (category);

-- Index for source filtering
CREATE INDEX IF NOT EXISTS idx_memories_source_type
    ON memories (source_type);

-- Index for source_machine filtering
CREATE INDEX IF NOT EXISTS idx_memories_source_machine
    ON memories (source_machine);

-- Index for extraction_status (backfill queries)
CREATE INDEX IF NOT EXISTS idx_memories_extraction_status
    ON memories (extraction_status);

-- Index for quality scoring queries (find unscored memories)
CREATE INDEX IF NOT EXISTS idx_memories_quality_score
    ON memories (quality_score) WHERE quality_score IS NULL;

-- Full-text search on content
CREATE INDEX IF NOT EXISTS idx_memories_content_fts
    ON memories USING gin (to_tsvector('english', content));

-- Index for timeline / recency queries
CREATE INDEX IF NOT EXISTS idx_memories_created_at
    ON memories (created_at DESC);

-- Raw conversation archives
CREATE TABLE IF NOT EXISTS conversations_raw (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    source_type     text NOT NULL,
    source_file     text,
    title           text,
    messages        jsonb NOT NULL,
    message_count   int,
    imported_at     timestamptz DEFAULT now(),
    metadata        jsonb DEFAULT '{{}}'::jsonb
);

-- ──────────────────────────────────────────────
-- Knowledge graph: entities
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entities (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    name            text NOT NULL,
    entity_type     text NOT NULL,
    canonical_name  text NOT NULL,
    description     text,
    embedding       vector({settings.embedding_dimensions}),
    mention_count   integer DEFAULT 1,
    metadata        jsonb DEFAULT '{{}}'::jsonb,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

-- Unique constraint on canonical entity identity
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_entities_canonical'
    ) THEN
        ALTER TABLE entities ADD CONSTRAINT uq_entities_canonical
            UNIQUE (canonical_name, entity_type);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_entities_embedding_hnsw
    ON entities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_entities_type
    ON entities (entity_type);

-- ──────────────────────────────────────────────
-- Entity-memory junction
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity_memories (
    memory_id       uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id       uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role            text NOT NULL DEFAULT 'mention',
    confidence      real DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id, role)
);

CREATE INDEX IF NOT EXISTS idx_entity_memories_entity
    ON entity_memories (entity_id);

-- ──────────────────────────────────────────────
-- Entity relationships (knowledge graph edges)
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity_relations (
    id                  uuid DEFAULT uuidv7() PRIMARY KEY,
    source_entity_id    uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id    uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relationship_type   text NOT NULL,
    confidence          real DEFAULT 1.0,
    source_memory       uuid REFERENCES memories(id) ON DELETE SET NULL,
    properties          jsonb DEFAULT '{{}}'::jsonb,
    valid               boolean DEFAULT true,
    created_at          timestamptz DEFAULT now()
);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_entity_relations'
    ) THEN
        ALTER TABLE entity_relations ADD CONSTRAINT uq_entity_relations
            UNIQUE (source_entity_id, target_entity_id, relationship_type);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_entity_relations_source
    ON entity_relations (source_entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_relations_target
    ON entity_relations (target_entity_id);

-- ──────────────────────────────────────────────
-- Agent events (activity log)
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_events (
    id                  uuid DEFAULT uuidv7() PRIMARY KEY,
    agent_id            text,
    session_id          text,
    event_type          text NOT NULL,
    category            text,
    description         text,
    related_memory_ids  uuid[],
    metadata            jsonb DEFAULT '{{}}'::jsonb,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_events_type
    ON agent_events (event_type);

CREATE INDEX IF NOT EXISTS idx_agent_events_created
    ON agent_events (created_at DESC);

-- GIN index for metadata filtering on agent_events
CREATE INDEX IF NOT EXISTS idx_agent_events_metadata
    ON agent_events USING gin (metadata);

-- ──────────────────────────────────────────────
-- Memory outcomes (feedback tracking)
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_outcomes (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    memory_id       uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    agent_id        text,
    session_id      text,
    was_useful      boolean NOT NULL,
    context         text,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_outcomes_memory
    ON memory_outcomes (memory_id);

-- ──────────────────────────────────────────────
-- Drop unused legacy table
-- ──────────────────────────────────────────────
DROP TABLE IF EXISTS memory_relations;

-- ──────────────────────────────────────────────
-- Memory versions (full audit trail / time machine)
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_versions (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    memory_id       uuid NOT NULL,  -- no FK: memory may be deleted
    version         int NOT NULL,
    -- Full snapshot at this version
    content         text NOT NULL,
    summary         text,
    tags            text[] DEFAULT '{{}}'::text[],
    category        text,
    confidence      real,
    metadata        jsonb,
    -- What happened to create this version
    change_type     text NOT NULL,  -- created, dedup_update, dedup_supersede, manual_update, manual_delete, consolidation, auto_summarize, quality_score, decay_archive, restore
    change_reason   text,           -- LLM reasoning or human explanation
    -- Provenance
    changed_by      text,           -- 'agent', 'scheduler:<job>', 'manual', 'mcp'
    source_memory_id uuid,          -- for merges: the incoming memory that triggered change
    similarity_score real,          -- for dedup: cosine similarity that matched
    -- Quick-scan flags
    content_changed  boolean DEFAULT false,
    tags_changed     boolean DEFAULT false,
    category_changed boolean DEFAULT false,
    created_at      timestamptz DEFAULT now() NOT NULL,
    UNIQUE(memory_id, version)
);

CREATE INDEX IF NOT EXISTS idx_memory_versions_memory
    ON memory_versions (memory_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_memory_versions_time
    ON memory_versions (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_versions_type
    ON memory_versions (change_type);

-- ──────────────────────────────────────────────
-- Functions
-- ──────────────────────────────────────────────

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

DROP TRIGGER IF EXISTS trg_entities_updated_at ON entities;
CREATE TRIGGER trg_entities_updated_at
    BEFORE UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Composite relevance scoring function (v3: quality-based, no access-count dependency)
CREATE OR REPLACE FUNCTION memory_relevance(
    query_embedding vector({settings.embedding_dimensions}),
    mem_embedding vector({settings.embedding_dimensions}),
    mem_created_at timestamptz,
    mem_importance real,
    mem_stability real,
    mem_access_count integer DEFAULT 0,
    current_ts timestamptz DEFAULT now(),
    mem_quality_score real DEFAULT NULL
) RETURNS real AS $$
DECLARE
    cosine_sim real;
    recency_boost real;
    quality real;
BEGIN
    -- Cosine similarity (0..1)
    cosine_sim := 1.0 - (query_embedding <=> mem_embedding);

    -- Slight recency boost, half-life ~180 days (gentle, not punishing)
    recency_boost := EXP(-0.00385 * EXTRACT(EPOCH FROM (current_ts - mem_created_at)) / 86400.0);

    -- Quality: use LLM-assessed score if available, else assume neutral (0.5)
    quality := COALESCE(mem_quality_score, 0.5);

    -- 65% similarity + 15% quality + 10% importance + 10% recency
    RETURN (0.65 * cosine_sim)
         + (0.15 * quality)
         + (0.10 * COALESCE(mem_importance, 0.5))
         + (0.10 * recency_boost);
END;
$$ LANGUAGE plpgsql STABLE;
"""


async def init_schema(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
