"""Database schema initialization and migrations."""

from nobrainr.config import settings

SCHEMA_SQL = f"""
-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

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
-- v3b: Fact-augmented key expansion (LongMemEval pattern) — LLM-generated
--      alternative search keyphrases stored alongside content so memories
--      are findable from more query angles even with lazy single-word searches.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_keys TEXT;
-- Contextual BM25 column (2026-04-19, Anthropic contextual retrieval). The
-- contextual prefix is already prepended to the EMBEDDING input — this
-- column routes the same prefix into the FTS GIN index so keyword queries
-- also benefit from chunk-situating context. Documented 35% → 49% failure
-- reduction when both embeddings AND BM25 see the prefix, not just embed.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS fts_context TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_specificity smallint;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_actionability smallint;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS quality_self_containment smallint;

-- v4: Embedding versioning — track which model generated each embedding
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_model text DEFAULT '{settings.embedding_model}';
ALTER TABLE entities ADD COLUMN IF NOT EXISTS embedding_model text DEFAULT '{settings.embedding_model}';

-- v5: Memory tiering (0=pinned, 1=hot, 2=standard, 3=cold)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS tier smallint DEFAULT 2;
CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories (tier);

-- HNSW index on halfvec for faster ANN search (~50% smaller than full-vector index).
-- qwen3-embedding-cpu outputs unit-norm 1024d vectors, so halfvec loses negligible
-- precision vs vector. Two indexes are maintained:
--   * The full index covers include_cold=True searches (rare; used by a few
--     GraphRAG paths).
--   * The partial index (tier < 3) is what every day-to-day hybrid search
--     actually walks — smaller graph, faster traversal, and pgvector 0.8+
--     knows to prefer it when the query's WHERE clause narrows to tier<3.
DROP INDEX IF EXISTS idx_memories_embedding_hnsw;
CREATE INDEX IF NOT EXISTS idx_memories_embedding_halfvec_hnsw
    ON memories USING hnsw ((embedding::halfvec({settings.embedding_dimensions})) halfvec_cosine_ops)
    WITH (m = 24, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_memories_embedding_halfvec_hnsw_hot
    ON memories USING hnsw ((embedding::halfvec({settings.embedding_dimensions})) halfvec_cosine_ops)
    WITH (m = 24, ef_construction = 200)
    WHERE tier < 3 AND embedding IS NOT NULL;

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

-- Full-text search on content. Uses 'simple' dictionary + unaccent so
-- German/Dutch/Spanish/etc. memories index correctly. A wrapper expression
-- is required (unaccent is not IMMUTABLE by default) — we create an
-- IMMUTABLE wrapper and index through it. Old English-only index is
-- dropped explicitly since its name did not previously match this one.
DROP INDEX IF EXISTS idx_memories_content_fts_en;
-- IMMUTABLE wrapper around unaccent(). The single-arg form uses the default
-- 'unaccent' dictionary; the two-arg form needs a regdictionary, not text.
-- STABLE would be accurate (unaccent itself is STABLE because it reads the
-- dictionary) but we mark IMMUTABLE here so postgres lets us use it in index
-- expressions — the dictionary is effectively read-only at runtime.
-- IMPORTANT: schema-qualify 'public.unaccent' because CREATE INDEX runs with
-- a restricted search_path and would otherwise fail with
-- "function unaccent(text) does not exist".
CREATE OR REPLACE FUNCTION nb_unaccent(text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $$ SELECT public.unaccent($1) $$;
-- Contextual BM25: the FTS index now includes fts_context (situational
-- prefix) alongside content + search_keys (fact-augmented key expansion).
-- Dropping and recreating the old index is safe — any query that reads the
-- index will fall through to a seq scan for the few seconds of the rebuild.
DROP INDEX IF EXISTS idx_memories_content_fts;
CREATE INDEX IF NOT EXISTS idx_memories_content_fts
    ON memories USING gin (
        to_tsvector(
            'simple',
            nb_unaccent(
                content || ' '
                || COALESCE(search_keys, '') || ' '
                || COALESCE(fts_context, '')
            )
        )
    );

-- Trigram index for fast ILIKE / similarity() fallback (names, short queries)
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm
    ON memories USING gin (content gin_trgm_ops);

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

DROP INDEX IF EXISTS idx_entities_embedding_hnsw;
CREATE INDEX IF NOT EXISTS idx_entities_embedding_halfvec_hnsw
    ON entities USING hnsw ((embedding::halfvec({settings.embedding_dimensions})) halfvec_cosine_ops)
    WITH (m = 24, ef_construction = 200);

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

-- Phase K: Priority cascade tiers for facts
-- tier: 1=canonical (verified, overrides vector), 2=historical, 3=derived (default)
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS tier smallint DEFAULT 3;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS verified_at timestamptz;
ALTER TABLE entity_relations ADD COLUMN IF NOT EXISTS verified_by text;
CREATE INDEX IF NOT EXISTS idx_entity_relations_tier
    ON entity_relations (tier) WHERE tier = 1;

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
-- Memory write queue (2026-04-11 v7)
-- ──────────────────────────────────────────────
-- Why this exists: memory_store MCP calls used to synchronously wait on
-- decide_write_action() which calls ollama_chat(), which inherits a 600s
-- default timeout from DEFAULT_LLM_TIMEOUT. Under GPU contention (scheduler
-- LLM jobs + search reranker + this classifier call all fighting for
-- llama-server), memory_store would blow through the MCP client timeout and
-- writes would silently vanish — exact symptom observed on 2026-04-11 when
-- lesson_classifier fired for the first time.
--
-- The queue decouples ACCEPTANCE from PROCESSING. The MCP tool now does one
-- INSERT into memory_write_queue (<50ms) and returns a queue_id. A dedicated
-- scheduler worker (memory_write_worker) drains the queue serially through
-- the same store_memory_with_extraction pipeline, sharing the scheduler's
-- _llm_semaphore so it never dogpiles the GPU alongside periodic LLM jobs.
CREATE TABLE IF NOT EXISTS memory_write_queue (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,

    -- Payload — every arg of store_memory_with_extraction captured as-is
    content         text NOT NULL,
    summary         text,
    tags            text[],
    category        text,
    source_type     text DEFAULT 'manual',
    source_machine  text,
    source_ref      text,
    confidence      real DEFAULT 1.0,
    metadata        jsonb,
    skip_dedup      boolean DEFAULT false,
    contextual_prefix text,

    -- Queue state machine: pending → processing → (done | failed | pending-retry)
    status          text NOT NULL DEFAULT 'pending',
    attempts        int NOT NULL DEFAULT 0,
    max_attempts    int NOT NULL DEFAULT 3,
    error_message   text,

    -- Result (populated when status='done')
    memory_id       uuid,
    result_status   text,  -- stored | updated | superseded | skipped

    -- Timestamps
    enqueued_at     timestamptz NOT NULL DEFAULT now(),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    completed_at    timestamptz
);

-- Partial index on pending rows — the worker's hot query hits this.
-- Ordered by next_attempt_at so exponential-backoff retries respect the delay.
CREATE INDEX IF NOT EXISTS idx_memory_write_queue_pending
    ON memory_write_queue (next_attempt_at)
    WHERE status = 'pending';

-- For the dashboard queue-depth widget
CREATE INDEX IF NOT EXISTS idx_memory_write_queue_enqueued
    ON memory_write_queue (enqueued_at DESC);

-- Retention sweep can use this to drop old done/failed rows
CREATE INDEX IF NOT EXISTS idx_memory_write_queue_completed
    ON memory_write_queue (completed_at)
    WHERE status IN ('done', 'failed');

-- ──────────────────────────────────────────────
-- Memory outcomes (feedback tracking)
-- ──────────────────────────────────────────────
-- query_trace_id / query_text / result_rank are v6 additions (2026-04-11):
-- they let us link a piece of feedback back to the search that surfaced the
-- memory so we can compute MRR/NDCG and reason about rank quality instead
-- of just raw positive/negative ratios. All three are nullable — pre-v6
-- feedback and manual dashboard thumbs-down won't carry trace metadata.
CREATE TABLE IF NOT EXISTS memory_outcomes (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    memory_id       uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    agent_id        text,
    session_id      text,
    was_useful      boolean NOT NULL,
    context         text,
    query_trace_id  uuid,
    query_text      text,
    result_rank     int,
    created_at      timestamptz DEFAULT now()
);

-- v6: add the trace columns on existing deployments
ALTER TABLE memory_outcomes ADD COLUMN IF NOT EXISTS query_trace_id uuid;
ALTER TABLE memory_outcomes ADD COLUMN IF NOT EXISTS query_text text;
ALTER TABLE memory_outcomes ADD COLUMN IF NOT EXISTS result_rank int;

CREATE INDEX IF NOT EXISTS idx_memory_outcomes_memory
    ON memory_outcomes (memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_outcomes_trace
    ON memory_outcomes (query_trace_id)
    WHERE query_trace_id IS NOT NULL;

-- ──────────────────────────────────────────────
-- Retrieval eval harness (2026-04-18)
-- ──────────────────────────────────────────────
-- A hand-labeled golden set + per-run metrics so we catch retrieval
-- regressions when changing embeddings, rerankers, or the RRF/HNSW
-- parameters. Binary relevance: each query has a list of memory_ids
-- that MUST be present in the top-K for the query to count as "hit."
CREATE TABLE IF NOT EXISTS eval_golden_queries (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    query           text NOT NULL,
    expected_ids    uuid[] NOT NULL,
    notes           text,
    tags            text[] DEFAULT '{{}}'::text[],
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eval_golden_active
    ON eval_golden_queries (active) WHERE active;

-- One row per full eval sweep. per_query holds the breakdown so we can
-- tell WHICH query regressed, not just that the mean recall dropped.
CREATE TABLE IF NOT EXISTS eval_runs (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    ran_at          timestamptz DEFAULT now(),
    model_tag       text,       -- e.g. "qwen3.6-35b" / "qwen3.5-legacy"
    embedding_model text,
    reranker_model  text,
    query_count     int NOT NULL,
    recall_at_10    real,
    mrr             real,
    ndcg_at_10      real,
    per_query       jsonb NOT NULL DEFAULT '[]'::jsonb,
    config          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    notes           text
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_ran_at
    ON eval_runs (ran_at DESC);

-- Extraction eval (qwen3.6 vs qwen3.5 A/B, LLM-judge self-scored).
-- Each run re-extracts a sample of memories with the candidate model
-- and compares entities + relationships against the on-disk extraction
-- produced by the incumbent. Judge is the candidate itself (Qwen3.6 as
-- self-judge) — avoids Anthropic API per data-sovereignty policy.
CREATE TABLE IF NOT EXISTS extraction_eval_runs (
    id                  uuid DEFAULT uuidv7() PRIMARY KEY,
    ran_at              timestamptz DEFAULT now(),
    candidate_model     text NOT NULL,    -- "qwen3.6:35b"
    incumbent_model     text NOT NULL,    -- "qwen3.5:35b"
    sample_size         int  NOT NULL,
    entity_f1           real,             -- mean F1 of entity-name overlap
    relation_f1         real,             -- mean F1 of relationship-triplet overlap
    judge_score         real,             -- mean LLM-judge semantic-equivalence 0-1
    per_memory          jsonb NOT NULL DEFAULT '[]'::jsonb,
    config              jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    notes               text
);
CREATE INDEX IF NOT EXISTS idx_extraction_eval_runs_ran_at
    ON extraction_eval_runs (ran_at DESC);

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

-- Memory facts: atomic, searchable knowledge extracted from memories (Mem0-style)
CREATE TABLE IF NOT EXISTS memory_facts (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding vector({settings.embedding_dimensions}),
    embedding_model text DEFAULT '{settings.embedding_model}',
    quality_score REAL,
    created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS embedding_model text DEFAULT '{settings.embedding_model}';
-- Backfill any rows that existed before embedding_model column was added
UPDATE memory_facts SET embedding_model = '{settings.embedding_model}'
    WHERE embedding_model IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_memory ON memory_facts(memory_id);
CREATE INDEX IF NOT EXISTS idx_facts_created ON memory_facts(created_at DESC);

-- ──────────────────────────────────────────────
-- Procedural memory (Phase C G4, 2026-04-12, v6.8)
-- ──────────────────────────────────────────────
-- Letta + LangGraph "procedural memory" pattern: agent-writable rules and
-- instructions that affect future behavior. These are NOT retrieved by
-- similarity like regular memories — they're retrieved by SCOPE and applied
-- at session start or on demand. Use cases:
--
--   - Global: "always run tests before committing" (applies to all agents)
--   - Agent: "this agent prefers terse responses" (per-agent style)
--   - Project: "in project foo, tests live in tests/" (per-project convention)
--   - Session: temporary rules that expire with the session
--
-- The key difference from regular memories:
--   - Regular memory: facts, experiences, learnings (searched by similarity)
--   - Procedural memory: rules, instructions, preferences (loaded by scope)
--
-- Design decisions:
--   - Separate table (not a category on memories) so procedural memories
--     never appear in memory_search results and never compete with facts
--   - Soft-delete via active=false rather than DELETE, so rules leave an
--     audit trail of what was tried
--   - priority 0-100 for explicit ordering when multiple rules apply
--   - expires_at optional, lets session-scoped rules auto-deactivate
CREATE TABLE IF NOT EXISTS procedural_memories (
    id              uuid DEFAULT uuidv7() PRIMARY KEY,
    content         text NOT NULL,
    title           text,                        -- short label for quick lookup
    scope           text NOT NULL CHECK (scope IN ('global', 'agent', 'project', 'session')),
    agent_id        text,                        -- required when scope='agent'
    project_id      text,                        -- required when scope='project'
    session_id      text,                        -- required when scope='session'
    priority        int NOT NULL DEFAULT 50,     -- 0..100, higher applies first
    active          boolean NOT NULL DEFAULT true,
    tags            text[] DEFAULT '{{}}'::text[],
    metadata        jsonb DEFAULT '{{}}'::jsonb,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now(),
    expires_at      timestamptz                  -- NULL = never expires
);

-- Composite index for the default "get active rules ordered by priority" query
CREATE INDEX IF NOT EXISTS idx_procedural_scope_active
    ON procedural_memories (scope, active, priority DESC);

-- Partial indexes for scope-specific lookups (skip the index when NULL)
CREATE INDEX IF NOT EXISTS idx_procedural_agent
    ON procedural_memories (agent_id)
    WHERE agent_id IS NOT NULL AND active;
CREATE INDEX IF NOT EXISTS idx_procedural_project
    ON procedural_memories (project_id)
    WHERE project_id IS NOT NULL AND active;
CREATE INDEX IF NOT EXISTS idx_procedural_session
    ON procedural_memories (session_id)
    WHERE session_id IS NOT NULL AND active;

-- Index for expiry sweep
CREATE INDEX IF NOT EXISTS idx_procedural_expires
    ON procedural_memories (expires_at)
    WHERE expires_at IS NOT NULL AND active;

-- Updated_at trigger reuses the generic updater defined above
DROP TRIGGER IF EXISTS trg_procedural_updated_at ON procedural_memories;
CREATE TRIGGER trg_procedural_updated_at
    BEFORE UPDATE ON procedural_memories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ──────────────────────────────────────────────
-- Bi-temporal validity windows on memory_facts (Phase K, v6.15, 2026-04-12)
-- ──────────────────────────────────────────────
-- Zep / Graphiti pattern: every fact carries two timestamps —
-- valid_from (when we started believing it) and valid_to (when we
-- stopped). Supersession becomes a soft-delete: set the old fact's
-- valid_to=now() and INSERT a new fact with valid_from=now(), rather
-- than physically replacing the row. This unlocks point-in-time
-- queries ("what did we believe on date X?") while keeping the current
-- view clean by filtering to valid_to IS NULL.
--
-- Additive migration — ADD COLUMN with DEFAULT now() + nullable
-- valid_to is metadata-only on Postgres 11+ so it applies instantly
-- even to large tables. Existing rows get valid_from=now() on
-- migration (they become "valid from the moment we added the column"),
-- which is the right default because we have no earlier truth to
-- reference.
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS valid_from timestamptz NOT NULL DEFAULT now();
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS valid_to timestamptz;

-- Composite index on the validity window for point-in-time lookups.
-- Covers both "what is valid now" (valid_to IS NULL) and "what was
-- valid on date X" (valid_from <= X AND valid_to > X) queries.
CREATE INDEX IF NOT EXISTS idx_memory_facts_valid_range
    ON memory_facts (valid_from, valid_to);

-- Phase K: Priority tier for memory_facts (1=canonical, 2=historical, 3=derived)
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS tier smallint DEFAULT 3;
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS verified_at timestamptz;
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS verified_by text;
CREATE INDEX IF NOT EXISTS idx_memory_facts_tier
    ON memory_facts (tier) WHERE tier = 1;

-- ──────────────────────────────────────────────
-- Memory tombstones (Phase H, 2026-04-12, v6.10)
-- ──────────────────────────────────────────────
-- doobidoo-inspired pattern: when a memory is deleted, record a hash
-- of its content. The write-queue dedup classifier (decide_write_action)
-- consults this table BEFORE doing similarity search + LLM decision —
-- if the hash matches, it short-circuits to NOOP with reason='tombstoned'.
-- This prevents re-ingestion of a memory the user explicitly deleted
-- (e.g. same document re-crawled, same ChatGPT export re-imported, same
-- document queue replay). The hash is stable across leading/trailing
-- whitespace + case variations so near-duplicates hit too.
CREATE TABLE IF NOT EXISTS memory_tombstones (
    id                  uuid DEFAULT uuidv7() PRIMARY KEY,
    content_hash        text NOT NULL UNIQUE,
    original_memory_id  uuid,
    reason              text NOT NULL DEFAULT 'manual_delete',
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_tombstones_created
    ON memory_tombstones (created_at DESC);
"""


async def init_schema(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
