"""Configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "NOBRAINR_"}

    # PostgreSQL
    database_url: str = "postgresql://nobrainr:nobrainr@localhost:5432/nobrainr"

    # LLM inference (llama-server for GPU, Ollama for embeddings)
    llm_server_url: str = "http://llama-server:8080"
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "qwen3-embedding-cpu"
    # Additional labels that refer to bit-identical vectors. The search
    # safeguard matches ANY of these so runtime-only tag drift (cpu vs gpu,
    # :0.6b vs no suffix, :latest) never silently hides 95% of memories
    # again the way it did on 2026-04-08.
    embedding_model_aliases: list[str] = [
        "qwen3-embedding-cpu",
        "qwen3-embedding:0.6b",
        "qwen3-embedding",
        "qwen3-embedding:latest",
        "qwen3-embedding-cpu:latest",
    ]
    embedding_dimensions: int = 1024

    # MCP Server
    host: str = "0.0.0.0"
    port: int = 8420

    # Search defaults
    default_search_limit: int = 10
    default_similarity_threshold: float = 0.3

    # Crawl4AI
    crawl4ai_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: str = ""

    # Speaches (OpenAI-compatible whisper API)
    speaches_url: str = "http://speaches:8000"
    speaches_model: str = "whisper-large-v3"
    speaches_tts_model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX-fp16"
    speaches_tts_voice: str = "af_heart"  # Kokoro default voice

    # Chunked ingestion
    chunk_max_chars: int = 6000  # max chars per chunk
    chunk_overlap_chars: int = 500  # overlap between consecutive chunks
    chunk_threshold: int = 8000  # content above this length gets chunked
    chunk_context_window: int = 1  # fetch N adjacent chunks around search hits
    contextual_embeddings_enabled: bool = True  # prepend LLM-generated context to chunks before embedding

    # Reranking.
    #  - "sentence-transformers" (default): BAAI/bge-reranker-v2-m3, 100+ languages,
    #    cross-encoder, ~560MB, CPU inference ~200ms for 150 docs. Best quality.
    #  - "flashrank": ONNX, English-only MiniLM, ~100ms for 30 docs. Fallback for
    #    environments that can't install torch.
    reranker_enabled: bool = True
    # Backend preference order. "http" hits a dedicated TEI sidecar
    # (ghcr.io/huggingface/text-embeddings-inference), keeps the backend
    # container thin and model updates independent. Falls through to
    # local sentence-transformers (then flashrank) on HTTP failure so
    # the reranker stays available if the sidecar is mid-deploy.
    reranker_backend: str = "http"
    # URL of the TEI sidecar. Resolved in-cluster via Docker DNS on the
    # `mcp` network (alias `reranker`). Override with NOBRAINR_RERANKER_URL
    # if routing differs per deploy.
    reranker_url: str = "http://reranker:80"
    reranker_http_timeout_s: float = 30.0
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_fallback_model: str = "ms-marco-MiniLM-L-12-v2"  # flashrank name
    # Cap concurrent cross-encoder runs. i5-13500 = 20 threads but a single
    # bge-reranker-v2-m3 call on 150 docs spins 10+ cores. Set to 2 so a
    # batch caller can't lock out interactive search. See incident
    # 2026-04-19: 30-query eval sweep pegged CPU at 1063% for minutes.
    reranker_concurrency: int = 2
    # Hard cap on how long a rerank can wait for the semaphore before it
    # gives up and returns pre-rerank order. Keeps interactive search
    # responsive under any batch load.
    reranker_queue_timeout_s: float = 10.0
    # Max candidates sent to the cross-encoder. BGE-reranker-v2-m3 on CPU
    # Candle is ~1-2s per real-memory text; 8 fits reliably inside the 20s
    # search_hard_timeout_s even under GPU/CPU contention from extraction.
    # Replaces the old "full 150 candidates Anthropic recipe" approach which
    # worked only with GPU — blocked here by Qwen3.6-35B VRAM reservation.
    # 4-branch RRF already does strong upstream selection so the quality
    # delta vs 150 is modest on this hybrid pipeline. Raise to 30+ if TEI
    # gets a GPU slot (requires reducing Qwen --ctx-size to free VRAM).
    reranker_max_candidates: int = 8

    # Reranker skip-when-dominant (2026-04-19). If top-1 RRF score is
    # ≥ rerank_skip_dominance_ratio × top-2 score, skip the reranker —
    # the cross-encoder can't meaningfully reorder when RRF already has
    # a clear winner. Saves CPU on easy queries so the reranker is free
    # when it actually matters. 1.8× is empirically dominant.
    rerank_skip_when_dominant: bool = True
    # Raised from 1.8 → 2.5 (2026-04-19): 1.8 caused some rank-1 hits
    # that the reranker would have promoted from rank-2 to lose out. A
    # 2.5× gap means RRF had a truly dominant winner and the reranker
    # really can't change it.
    rerank_skip_dominance_ratio: float = 2.5

    # Graph-aware 4th RRF branch (2026-04-19, HippoRAG-lite). Fuzzy-matches
    # query terms against entities.canonical_name via pg_trgm and lifts
    # memories that are linked to matched entities in the knowledge graph.
    # Captures most of the associative-memory lift (HippoRAG 2 reported +7%
    # on multi-hop tasks) without running full Personalized PageRank.
    graph_branch_enabled: bool = True
    # Short queries match too many trigram candidates and slow the branch
    # down for no quality gain. Skip if query is below this.
    graph_branch_min_query_chars: int = 8
    # pg_trgm similarity threshold for entity fuzzy match. 0.5 empirically
    # (2026-04-19 eval): 0.3 was too loose and pulled semi-matching
    # entities that RRF-boosted irrelevant memories past the correct
    # rank-1 hits, causing a -6pp Recall@10 regression. 0.5 requires real
    # word-level overlap before the graph branch fires.
    graph_branch_trigram_threshold: float = 0.5
    # Max entities to consider after trigram match.
    graph_branch_max_entities: int = 10
    # Graph branch contributes at this RRF weight — kept below 0.5 so
    # strong vector + FTS hits keep ownership of the top ranks; graph
    # branch is additive signal for multi-hop, not a prior.
    graph_branch_rrf_weight: float = 0.35

    # End-to-end deadline for memory_search. Above this we short-circuit
    # expensive stages (rerank, related_memories, chunk_context) and return
    # whatever we have with a `quality_tier` tag so the caller sees it
    # got a degraded result instead of a hang. 20s is generous for the
    # happy path but ruthless under load.
    search_hard_timeout_s: float = 20.0
    # Skip the reranker if we've already burned this fraction of the
    # budget on earlier stages. 0.75 = if ≥15s used out of 20s, skip.
    search_rerank_budget_frac: float = 0.75

    # Security
    cors_origins: list[str] = ["http://localhost:8420"]
    max_content_length: int = 50000  # 50KB max memory content

    # Extraction (knowledge graph)
    extraction_model: str = "qwen3.6:35b"
    extraction_enabled: bool = True

    # Chat (RAG)
    chat_model: str = ""  # defaults to extraction_model
    chat_max_context_memories: int = 5  # reduced from 15→10→5 for fast chat (~10s prefill)
    chat_max_source_memories: int = 15  # reduced from 50 — less entity noise in UI
    chat_max_message_length: int = 2000
    chat_max_history_length: int = 20
    chat_enabled: bool = True

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_llm_concurrency: int = 5  # max concurrent LLM jobs (leave slots for live requests)
    scheduler_inter_request_delay: float = 1.0  # seconds between LLM calls in batch jobs
    maintenance_interval_hours: float = 6.0
    feedback_interval_hours: float = 12.0

    # Machine identifier for scheduler-created memories (defaults to hostname)
    source_machine: str = ""

    # LLM scheduler jobs — BALANCED MODE: fact extraction + graph evolution
    scheduler_llm_model: str = "qwen3.6:35b"
    summarize_interval_hours: float = 1.0
    summarize_batch_size: int = 20
    consolidation_interval_hours: float = 2.0
    consolidation_batch_size: int = 10
    synthesis_interval_hours: float = 3.0
    synthesis_batch_size: int = 5
    entity_enrichment_interval_hours: float = 2.0
    entity_enrichment_batch_size: int = 20
    entity_merging_interval_hours: float = 2.0
    entity_merging_batch_size: int = 15
    insight_extraction_interval_hours: float = 1.0
    insight_extraction_batch_size: int = 30
    chatgpt_distill_interval_hours: float = 2.0  # 2h — backlog done, free GPU for fact extraction
    chatgpt_distill_batch_size: int = 15  # reduced: ~45min/run, releases LLM lock sooner
    chatgpt_distill_concurrency: int = 1
    chatgpt_distill_model: str = "qwen3.6:35b"
    # Fact extraction (NEW — Mem0-style atomic facts)
    fact_extraction_interval_hours: float = 0.05  # 3min — aggressive during backfill
    # Cut 200 → 20 on 2026-04-20: a 200-memory batch holds the LLM semaphore
    # for 10-30min, starving quality_scoring + all other LLM jobs (observed:
    # quality_scoring last_run was 6h old with 33K unscored backlog). With
    # batch=20 the extraction loop yields every ~2min, letting sibling jobs
    # get their turn. Total throughput similar since the fairness yield
    # sleeps only 5s between batches when backlog exists.
    fact_extraction_batch_size: int = 200
    # Memory decay
    decay_interval_hours: float = 24.0
    decay_batch_size: int = 50
    # Contradiction detection
    contradiction_interval_hours: float = 4.0
    contradiction_batch_size: int = 10
    # Cross-machine insights
    cross_machine_interval_hours: float = 6.0
    cross_machine_batch_size: int = 5
    # Extraction quality
    quality_interval_hours: float = 4.0
    quality_batch_size: int = 20
    # Memory quality scoring (LLM-assessed) — aggressive to clear 78% unscored backlog
    quality_scoring_interval_hours: float = 0.25
    quality_scoring_batch_size: int = 60
    # Fact-augmented key expansion (LongMemEval pattern)
    key_expansion_interval_hours: float = 0.5
    key_expansion_batch_size: int = 30
    # Knowledge crawl
    knowledge_crawl_enabled: bool = True
    knowledge_crawl_interval_hours: float = 3.0
    knowledge_crawl_batch_size: int = 5
    knowledge_crawl_delay: float = 10.0  # seconds between requests (be polite)

    # Link discovery (Phase 2) — queue interesting links found during crawling
    link_discovery_enabled: bool = True
    link_discovery_max_per_page: int = 5  # max links to queue per crawled page
    link_discovery_min_score: float = 0.4  # minimum score to queue a link

    # Entity web research (Phase 3) — targeted crawling for underdescribed entities
    entity_research_enabled: bool = True
    entity_research_interval_hours: float = 6.0  # 6h (was 12h)
    entity_research_batch_size: int = 5  # (was 3)
    entity_research_min_mentions: int = 5  # only research entities with 5+ mentions
    entity_research_cooldown_days: int = 14  # don't re-research within 14 days

    # Freshness re-crawl (Phase 4) — update stale crawled memories
    freshness_enabled: bool = True
    freshness_interval_hours: float = 12.0  # 12h (was 24h)
    freshness_batch_size: int = 5  # (was 3)
    freshness_max_age_days: int = 30  # re-crawl pages older than 30 days

    # Interest tracking (Phase 5) — prioritize crawling based on usage signals
    interest_tracking_enabled: bool = True
    interest_expansion_interval_hours: float = 12.0  # 12h (was 24h)
    interest_expansion_batch_size: int = 5  # (was 3)
    interest_signal_decay_days: int = 30  # signals older than 30 days lose weight

    # System pulse (autonomous health transmissions)
    system_pulse_interval_hours: float = 24.0
    # Community detection (GraphRAP)
    community_detection_interval_hours: float = 6.0  # balanced
    # Auto-optimize (search quality self-improvement)
    auto_optimize_interval_hours: float = 12.0
    # Co-occurrence relationship inference
    cooccurrence_interval_hours: float = 2.0  # 2h — still aggressive but leaves room for facts
    cooccurrence_batch_size: int = 40
    # GitHub incremental sync
    github_owner: str = ""  # GitHub username for sync (required for github_sync job)
    github_sync_interval_hours: float = 4.0  # 4h — catches active dev within a work session
    github_sync_quality_gate: bool = True  # LLM-score commits before storing, skip noise

    # Lesson classifier — tier-2 LLM pass that adds the `lesson` tag to
    # memories the tier-1 SQL backfill (keyword/category/commit-prefix)
    # missed. Runs conservatively: only tags when LLM is_lesson=True AND
    # confidence>=4. See scheduler_jobs.lesson_classifier() for details.
    lesson_classifier_interval_hours: float = 6.0
    lesson_classifier_batch_size: int = 10

    # Retrieval eval harness (2026-04-18) — golden-set Recall@10/MRR/nDCG
    # runs periodically so we spot regressions when swapping embedding
    # models, rerankers, or RRF/HNSW parameters. Weekly default.
    retrieval_eval_interval_hours: float = 168.0  # 7 days
    retrieval_eval_k: int = 10

    # Extraction eval harness (2026-04-20) — A/B current vs prior LLM on
    # a sample of memories, comparing entity/relationship overlap +
    # self-judge semantic equivalence. Weekly. Set
    # NOBRAINR_EXTRACTION_EVAL_INCUMBENT_MODEL to the pre-swap model so
    # we can catch quality regressions before committing to a new one.
    extraction_eval_interval_hours: float = 168.0  # 7 days
    extraction_eval_sample_size: int = 10
    extraction_eval_incumbent_model: str = "qwen3.5:35b"

    # Auto-negative outcome logging (2026-04-18) — feedback loop was dead
    # because nothing ever wrote was_useful=false, so the importance adjuster
    # had no variance to learn from. memory_search now auto-logs negatives
    # when retrieval is weak, fed by the same trace_id the UI uses so the
    # scheduler feedback job can down-rank misfiring memories.
    auto_negative_outcomes_enabled: bool = True
    # Log negative for every surfaced result when <N results come back. Low
    # recall is a signal that none of them were strongly relevant.
    auto_negative_low_recall_threshold: int = 3
    # Log negative for top-1 when reranker score is below this. Threshold
    # picked from the memory_search default threshold (0.3) — below that
    # the cross-encoder is telling us "nothing here really matches."
    auto_negative_low_rerank_threshold: float = 0.3
    # Context string written to memory_outcomes.context so the signal is
    # distinguishable from human feedback in the scheduler feedback loop.
    auto_negative_context_prefix: str = "auto:"

    # Monitoring & Alerts
    monitoring_enabled: bool = True
    monitoring_interval_hours: float = 1.0
    monitoring_email_enabled: bool = False
    monitoring_smtp_host: str = ""
    monitoring_smtp_port: int = 587
    monitoring_smtp_user: str = ""
    monitoring_smtp_password: str = ""
    monitoring_smtp_from: str = ""
    monitoring_smtp_to: str = ""  # comma-separated recipients
    monitoring_unhealthy_threshold: int = 3  # consecutive unhealthy checks before alerting


settings = Settings()
