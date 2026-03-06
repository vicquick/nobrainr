"""Smoke test for configuration loading."""

from nobrainr.config import Settings


def test_default_settings():
    s = Settings()
    assert s.port == 8420
    assert s.embedding_model == "nomic-embed-text"
    assert s.embedding_dimensions == 768
    assert s.extraction_enabled is True
