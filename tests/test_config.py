"""Smoke test for configuration loading."""

from nobrainr.config import Settings


def test_default_settings():
    s = Settings()
    assert s.port == 8420
    # Defaults moved from nomic-embed-text (768d) to qwen3-embedding-cpu (1024d)
    # during the March 2026 embedding-model migration. Test was never updated.
    assert s.embedding_model == "qwen3-embedding-cpu"
    assert s.embedding_dimensions == 1024
    assert s.extraction_enabled is True
