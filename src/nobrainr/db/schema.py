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

-- v4: Embedding versioning — track which model generated each embedding
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_model text DEFAULT '{settings.embedding_model}';
ALTER TABLE entities ADD COLUMN IF NOT EXISTS embedding_model text DEFAULT '{settings.embedding_model}';

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

-- ──────────────────────────────────────────────
-- Memory versioning triggers (automatic audit trail)
-- ──────────────────────────────────────────────

-- Version 0: snapshot on INSERT
CREATE OR REPLACE FUNCTION memory_version_on_insert()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO memory_versions (
        memory_id, version, content, summary, tags, category,
        confidence, metadata, change_type, changed_by
    ) VALUES (
        NEW.id, 0, NEW.content, NEW.summary, NEW.tags, NEW.category,
        NEW.confidence, NEW.metadata,
        'created',
        COALESCE(current_setting('nobrainr.changed_by', true), 'system')
    );
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    -- Never block a memory INSERT because of versioning failure
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Snapshot OLD state before UPDATE or DELETE
CREATE OR REPLACE FUNCTION memory_version_on_change()
RETURNS TRIGGER AS $$
DECLARE
    next_ver int;
BEGIN
    SELECT COALESCE(MAX(version), -1) + 1 INTO next_ver
    FROM memory_versions WHERE memory_id = OLD.id;

    INSERT INTO memory_versions (
        memory_id, version, content, summary, tags, category,
        confidence, metadata, change_type, change_reason, changed_by,
        content_changed, tags_changed, category_changed
    ) VALUES (
        OLD.id, next_ver, OLD.content, OLD.summary, OLD.tags, OLD.category,
        OLD.confidence, OLD.metadata,
        COALESCE(current_setting('nobrainr.change_type', true), 'unknown'),
        current_setting('nobrainr.change_reason', true),
        COALESCE(current_setting('nobrainr.changed_by', true), 'unknown'),
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.content IS DISTINCT FROM NEW.content ELSE false END,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.tags IS DISTINCT FROM NEW.tags ELSE false END,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.category IS DISTINCT FROM NEW.category ELSE false END
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    -- Never block a mutation because of versioning failure
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memory_version_insert ON memories;
CREATE TRIGGER trg_memory_version_insert
    AFTER INSERT ON memories
    FOR EACH ROW EXECUTE FUNCTION memory_version_on_insert();

DROP TRIGGER IF EXISTS trg_memory_version_change ON memories;
CREATE TRIGGER trg_memory_version_change
    BEFORE UPDATE OR DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION memory_version_on_change();

-- ──────────────────────────────────────────────
-- Generic audit log (entities + entity_relations)
-- ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial PRIMARY KEY,
    table_name      text NOT NULL,
    row_id          text NOT NULL,
    operation       text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    old_data        jsonb,
    new_data        jsonb,
    changed_by      text,
    change_reason   text,
    transaction_id  bigint DEFAULT txid_current(),
    created_at      timestamptz DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_table_row
    ON audit_log (table_name, row_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_time
    ON audit_log (created_at DESC);

CREATE OR REPLACE FUNCTION audit_trigger_func()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO audit_log (table_name, row_id, operation, old_data, changed_by)
        VALUES (TG_TABLE_NAME, OLD.id::text, 'DELETE',
                to_jsonb(OLD) - 'embedding',
                current_setting('nobrainr.changed_by', true));
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD IS NOT DISTINCT FROM NEW THEN RETURN NEW; END IF;
        INSERT INTO audit_log (table_name, row_id, operation, old_data, new_data, changed_by)
        VALUES (TG_TABLE_NAME, NEW.id::text, 'UPDATE',
                to_jsonb(OLD) - 'embedding',
                to_jsonb(NEW) - 'embedding',
                current_setting('nobrainr.changed_by', true));
        RETURN NEW;
    ELSIF TG_OP = 'INSERT' THEN
        INSERT INTO audit_log (table_name, row_id, operation, new_data, changed_by)
        VALUES (TG_TABLE_NAME, NEW.id::text, 'INSERT',
                to_jsonb(NEW) - 'embedding',
                current_setting('nobrainr.changed_by', true));
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_entities ON entities;
CREATE TRIGGER trg_audit_entities
    AFTER INSERT OR UPDATE OR DELETE ON entities
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

DROP TRIGGER IF EXISTS trg_audit_entity_relations ON entity_relations;
CREATE TRIGGER trg_audit_entity_relations
    AFTER INSERT OR UPDATE OR DELETE ON entity_relations
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

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
