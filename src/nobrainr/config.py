"""Configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "NOBRAINR_"}

    # PostgreSQL
    database_url: str = "postgresql://nobrainr:nobrainr@localhost:5432/nobrainr"

    # Ollama
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "snowflake-arctic-embed2"
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

    # Reranking
    reranker_enabled: bool = True
    reranker_model: str = "ms-marco-MiniLM-L-12-v2"

    # Security
    cors_origins: list[str] = ["http://localhost:8420"]
    max_content_length: int = 50000  # 50KB max memory content

    # Extraction (knowledge graph)
    extraction_model: str = "gemma3:12b"
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
    scheduler_llm_model: str = "gemma3:12b"
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
    chatgpt_distill_batch_size: int = 30
    chatgpt_distill_concurrency: int = 1
    chatgpt_distill_model: str = "gemma3:12b"
    # Fact extraction (NEW — Mem0-style atomic facts)
    fact_extraction_interval_hours: float = 0.5  # 30min — high priority during initial backfill
    fact_extraction_batch_size: int = 20
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
    # Memory quality scoring (LLM-assessed)
    quality_scoring_interval_hours: float = 0.5
    quality_scoring_batch_size: int = 30
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
