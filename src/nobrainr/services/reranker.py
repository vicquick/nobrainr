"""Cross-encoder reranking for search results.

Uses flashrank (ONNX-based, CPU-only) to rerank vector search results
with a cross-encoder model.  The model is loaded lazily on first use
and cached for subsequent calls.

Requires the ``reranker`` extra: ``pip install nobrainr[reranker]``

When flashrank is not installed, ``rerank()`` falls back to truncating
the input list (no-op reranking).
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

_FLASHRANK_AVAILABLE: bool | None = None


def _check_flashrank() -> bool:
    global _FLASHRANK_AVAILABLE
    if _FLASHRANK_AVAILABLE is None:
        try:
            import flashrank  # noqa: F401
            _FLASHRANK_AVAILABLE = True
        except ImportError:
            _FLASHRANK_AVAILABLE = False
            logger.warning(
                "flashrank not installed — reranking disabled. "
                "Install with: pip install nobrainr[reranker]"
            )
    return _FLASHRANK_AVAILABLE


@lru_cache(maxsize=1)
def _get_ranker():
    """Load the flashrank model (cached, thread-safe)."""
    from flashrank import Ranker
    logger.info("Loading reranker model: %s", settings.reranker_model)
    return Ranker(model_name=settings.reranker_model)


async def rerank(
    query: str,
    results: list[dict],
    *,
    limit: int = 10,
) -> list[dict]:
    """Rerank search results using a cross-encoder.

    Args:
        query: The original search query.
        results: List of memory dicts from search_memories().
        limit: Return top-N after reranking.

    Returns:
        Reranked list of memory dicts, trimmed to *limit*.
    """
    if not results or len(results) <= 1:
        return results[:limit]

    if not _check_flashrank():
        return results[:limit]

    ranker = _get_ranker()

    # Build passages for the reranker — use content (truncated) + summary
    from flashrank import RerankRequest

    passages = []
    for r in results:
        text = r.get("content", "")[:1000]
        summary = r.get("summary", "")
        if summary:
            text = f"{summary}\n\n{text}"
        passages.append({"id": r.get("id", ""), "text": text, "meta": r})

    request = RerankRequest(query=query, passages=passages)

    # flashrank is synchronous — run in executor to avoid blocking the event loop
    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(None, ranker.rerank, request)

    # Rebuild result list in reranked order
    reranked: list[dict] = []
    for item in ranked[:limit]:
        original = item.get("meta") or item["metadata"]
        original["rerank_score"] = round(float(item["score"]), 4)
        reranked.append(original)

    return reranked
