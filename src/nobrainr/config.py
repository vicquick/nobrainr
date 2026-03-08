"""Configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "NOBRAINR_"}

    # PostgreSQL
    database_url: str = "postgresql://nobrainr:nobrainr@localhost:5432/nobrainr"

    # Ollama
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768

    # MCP Server
    host: str = "0.0.0.0"
    port: int = 8420

    # Search defaults
    default_search_limit: int = 10
    default_similarity_threshold: float = 0.3

    # Crawl4AI
    crawl4ai_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: str = ""

    # Security
    cors_origins: list[str] = ["http://localhost:8420"]
    max_content_length: int = 50000  # 50KB max memory content

    # Extraction (knowledge graph)
    extraction_model: str = "gemma3:12b"
    extraction_enabled: bool = True

    # Chat (RAG)
    chat_model: str = ""  # defaults to extraction_model
    chat_max_context_memories: int = 15
    chat_max_source_memories: int = 50
    chat_max_message_length: int = 2000
    chat_max_history_length: int = 20
    chat_enabled: bool = True

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_llm_concurrency: int = 3  # max concurrent LLM jobs (leave slots for live requests)
    scheduler_inter_request_delay: float = 1.0  # seconds between LLM calls in batch jobs
    maintenance_interval_hours: float = 6.0
    feedback_interval_hours: float = 12.0

    # Machine identifier for scheduler-created memories (defaults to hostname)
    source_machine: str = ""

    # LLM scheduler jobs
    scheduler_llm_model: str = "gemma3:12b"
    summarize_interval_hours: float = 1.0
    summarize_batch_size: int = 20
    consolidation_interval_hours: float = 2.0
    consolidation_batch_size: int = 10
    synthesis_interval_hours: float = 4.0
    synthesis_batch_size: int = 5
    entity_enrichment_interval_hours: float = 2.0
    entity_enrichment_batch_size: int = 20
    entity_merging_interval_hours: float = 2.0
    entity_merging_batch_size: int = 15
    insight_extraction_interval_hours: float = 1.0
    insight_extraction_batch_size: int = 30
    chatgpt_distill_interval_hours: float = 0.1
    chatgpt_distill_batch_size: int = 20
    chatgpt_distill_model: str = "gemma3:12b"
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
    # Knowledge crawl
    knowledge_crawl_enabled: bool = True
    knowledge_crawl_interval_hours: float = 6.0
    knowledge_crawl_batch_size: int = 3  # pages per cycle
    knowledge_crawl_delay: float = 10.0  # seconds between requests (be polite)


settings = Settings()
