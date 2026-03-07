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

    # Security
    cors_origins: list[str] = ["*"]
    max_content_length: int = 50000  # 50KB max memory content

    # Extraction (knowledge graph)
    extraction_model: str = "qwen3:8b"
    extraction_enabled: bool = True

    # Scheduler
    scheduler_enabled: bool = True
    maintenance_interval_hours: float = 6.0
    feedback_interval_hours: float = 12.0

    # Machine identifier for scheduler-created memories (defaults to hostname)
    source_machine: str = ""

    # LLM scheduler jobs
    scheduler_llm_model: str = "qwen3:8b"
    summarize_interval_hours: float = 4.0
    summarize_batch_size: int = 10
    consolidation_interval_hours: float = 8.0
    consolidation_batch_size: int = 5
    synthesis_interval_hours: float = 24.0
    synthesis_batch_size: int = 3
    entity_enrichment_interval_hours: float = 12.0
    entity_enrichment_batch_size: int = 10
    insight_extraction_interval_hours: float = 6.0
    insight_extraction_batch_size: int = 20
    chatgpt_distill_interval_hours: float = 0.5
    chatgpt_distill_batch_size: int = 3
    chatgpt_distill_model: str = "qwen3:8b"


settings = Settings()
