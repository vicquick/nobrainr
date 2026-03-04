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

    # Extraction (knowledge graph)
    extraction_model: str = "qwen2.5:7b"
    extraction_enabled: bool = True

    # Scheduler
    scheduler_enabled: bool = True
    maintenance_interval_hours: float = 6.0
    feedback_interval_hours: float = 12.0

    # Dashboard
    dashboard_user: str = ""
    dashboard_password: str = ""


settings = Settings()
