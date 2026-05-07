"""Cross-encoder reranking for search results.

Two supported backends:

- **sentence-transformers** (default): ``BAAI/bge-reranker-v2-m3`` cross-encoder.
  Multilingual (100+ languages), best quality, ~560MB weights, ~200ms for 150 docs
  on CPU. Matches Anthropic's Contextual Retrieval recipe (top-150 → top-20) where
  recall is bounded by reranker quality rather than the vector candidate pool.

- **flashrank**: ONNX ``ms-marco-MiniLM-L-12-v2`` — English-only, ~100ms for 30 docs,
  ~34MB. Fallback for environments where torch cannot be installed.

The reranker selection is lazy: the primary backend is loaded on first call and
cached. If the primary backend fails to import (torch missing, model download
fails, etc.), the module transparently falls back to flashrank. If neither is
available, ``rerank()`` becomes a no-op (returns ``results[:limit]``).
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

# Probe results are memoised per process so we don't re-check imports on every
# search. Values: None=unchecked, True=available, False=unavailable.
_ST_AVAILABLE: bool | None = None
_FLASHRANK_AVAILABLE: bool | None = None

# Cap concurrent rerank calls. The BGE cross-encoder runs on CPU and each call
# processes up to 150 candidates through a transformer — saturates 10+ cores
# when stacked. On 2026-04-19 a 30-query eval sweep saturated the i5-13500 and
# starved live MCP memory_search for minutes (they queued behind reranking).
# Semaphore keeps interactive latency predictable at the cost of a little
# batch-job throughput. Settable via NOBRAINR_RERANKER_CONCURRENCY.
_RERANK_SEMAPHORE: asyncio.Semaphore | None = None


def _get_rerank_semaphore() -> asyncio.Semaphore:
    global _RERANK_SEMAPHORE
    if _RERANK_SEMAPHORE is None:
        _RERANK_SEMAPHORE = asyncio.Semaphore(settings.reranker_concurrency)
    return _RERANK_SEMAPHORE


def _check_sentence_transformers() -> bool:
    global _ST_AVAILABLE
    if _ST_AVAILABLE is None:
        try:
            import sentence_transformers  # noqa: F401
            _ST_AVAILABLE = True
        except ImportError:
            _ST_AVAILABLE = False
            logger.info("sentence-transformers not installed — trying flashrank fallback")
    return _ST_AVAILABLE


def _check_flashrank() -> bool:
    global _FLASHRANK_AVAILABLE
    if _FLASHRANK_AVAILABLE is None:
        try:
            import flashrank  # noqa: F401
            _FLASHRANK_AVAILABLE = True
        except ImportError:
            _FLASHRANK_AVAILABLE = False
            logger.warning(
                "flashrank not installed either — reranking will be a no-op"
            )
    return _FLASHRANK_AVAILABLE


@lru_cache(maxsize=1)
def _get_st_reranker():
    """Load the sentence-transformers CrossEncoder, cached for process lifetime.

    Tries GPU first (device='cuda'), falls back to CPU on OOM. BGE-v2-m3 is
    ~600MB; Qwen3-Reranker-0.6B is ~1.2GB at fp16. Both fit alongside
    Qwen3.6-27B when llama-server is idle (TTL 1h). When VRAM is full the
    cuda load fails → silent fall back to CPU.

    Qwen3-Reranker-0.6B (Apr 2026) is a drop-in for BGE-v2-m3 — the model
    card explicitly demonstrates `CrossEncoder("Qwen/Qwen3-Reranker-0.6B")`.
    It outputs raw logit differences (range ~-12 to +8) instead of [0,1]
    probabilities, so reranker_apply_sigmoid normalises to [0,1] keeping
    the auto_negative_low_rerank_threshold semantics consistent.
    """
    from sentence_transformers import CrossEncoder
    device = settings.reranker_device
    kwargs = {"max_length": 512, "device": device}
    # Qwen3-Reranker is 0.6B params — load in fp16 on GPU to fit alongside
    # the 19GB-resident llama-server. fp32 would be ~2.4GB and OOM on the
    # 20GB RTX 4000 SFF Ada when llama-server is active.
    if device == "cuda":
        try:
            import torch
            kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
        except Exception:
            pass
    try:
        logger.info(
            "Loading sentence-transformers reranker on %s: %s",
            device, settings.reranker_model,
        )
        return CrossEncoder(settings.reranker_model, **kwargs)
    except Exception as exc:
        if device == "cuda":
            logger.warning(
                "Reranker GPU load failed (%s); falling back to CPU", exc,
            )
            return CrossEncoder(settings.reranker_model, max_length=512, device="cpu")
        raise


@lru_cache(maxsize=1)
def _get_flashrank_reranker():
    """Load the flashrank ONNX ranker, cached for process lifetime."""
    from flashrank import Ranker
    logger.info("Loading flashrank reranker: %s", settings.reranker_fallback_model)
    return Ranker(model_name=settings.reranker_fallback_model)


def _build_passages(results: list[dict]) -> list[tuple[str, dict]]:
    """Turn raw search results into (text, meta) tuples for scoring.

    Prepends summary when present and truncates to ~1000 chars so we stay well
    under the cross-encoder's 512-token window after tokenisation.
    """
    passages: list[tuple[str, dict]] = []
    for r in results:
        text = r.get("content", "")[:1000]
        summary = r.get("summary", "")
        if summary:
            text = f"{summary}\n\n{text}"
        passages.append((text, r))
    return passages


async def _rerank_sentence_transformers(
    query: str,
    results: list[dict],
    limit: int,
) -> list[dict]:
    model = _get_st_reranker()
    passages = _build_passages(results)
    pairs = [(query, text) for text, _ in passages]

    # The underlying .predict() is blocking CPU work — push it off the event loop.
    loop = asyncio.get_running_loop()
    scores = await loop.run_in_executor(
        None,
        lambda: model.predict(pairs, batch_size=16, show_progress_bar=False, convert_to_numpy=True),
    )

    # Optionally normalise to [0,1]. Qwen3-Reranker outputs raw logits
    # roughly [-12, +8]; BGE-v2-m3 outputs raw logits in similar range.
    # Sigmoid keeps thresholds (auto_negative_low_rerank_threshold) stable
    # across reranker swaps so the feedback loop semantics don't drift.
    if settings.reranker_apply_sigmoid:
        import math
        scores = [1.0 / (1.0 + math.exp(-float(s))) for s in scores]

    scored = list(zip(scores, passages))
    scored.sort(key=lambda s: float(s[0]), reverse=True)

    reranked: list[dict] = []
    for score, (_text, meta) in scored[:limit]:
        meta["rerank_score"] = round(float(score), 4)
        reranked.append(meta)
    return reranked


async def _rerank_flashrank(
    query: str,
    results: list[dict],
    limit: int,
) -> list[dict]:
    from flashrank import RerankRequest

    ranker = _get_flashrank_reranker()
    passages_for_fr = []
    for text, meta in _build_passages(results):
        passages_for_fr.append({"id": meta.get("id", ""), "text": text, "meta": meta})

    request = RerankRequest(query=query, passages=passages_for_fr)
    loop = asyncio.get_running_loop()
    ranked = await loop.run_in_executor(None, ranker.rerank, request)

    reranked: list[dict] = []
    for item in ranked[:limit]:
        original = item.get("meta") or item["metadata"]
        original["rerank_score"] = round(float(item["score"]), 4)
        reranked.append(original)
    return reranked


async def _rerank_http(
    query: str,
    results: list[dict],
    limit: int,
) -> list[dict]:
    """Rerank via a remote text-embeddings-inference (TEI) sidecar.

    Posts {query, texts} to `<reranker_url>/rerank` and gets back a
    sorted list of {index, score}. Keeps the nobrainr backend image
    thin (no 560MB reranker weights baked in) and lets us upgrade the
    reranker independently of the backend deploy cycle.
    """
    import httpx

    # Cap candidates sent to the cross-encoder. BGE-reranker-v2-m3 on CPU
    # Candle is ~1-2s/doc on real memory texts — 150 blows the 20s search
    # budget. See config.reranker_max_candidates docstring for the full
    # trade-off write-up (RRF does upstream work, so cap is modest loss).
    capped = results[: settings.reranker_max_candidates]
    passages = _build_passages(capped)
    texts = [text for text, _meta in passages]
    url = settings.reranker_url.rstrip("/") + "/rerank"

    async with httpx.AsyncClient(timeout=settings.reranker_http_timeout_s) as client:
        resp = await client.post(url, json={"query": query, "texts": texts})
        resp.raise_for_status()
        scored = resp.json()

    # TEI returns list of {index, score} sorted by score desc
    reranked: list[dict] = []
    for item in scored[:limit]:
        idx = int(item["index"])
        if 0 <= idx < len(passages):
            _text, meta = passages[idx]
            meta["rerank_score"] = round(float(item["score"]), 4)
            reranked.append(meta)
    return reranked


async def rerank(
    query: str,
    results: list[dict],
    *,
    limit: int = 10,
) -> list[dict]:
    """Rerank search results using a cross-encoder.

    Backend selection is controlled by ``NOBRAINR_RERANKER_BACKEND``:

    - ``http`` (default from 2026-04-19): remote TEI sidecar, keeps the
      backend image slim. Falls through to local on HTTP failure.
    - ``sentence-transformers``: in-process multilingual BGE reranker.
    - ``flashrank``: English-only ONNX fallback.

    On any backend failure the function returns ``results[:limit]`` so search
    never fails because of the reranker.
    """
    if not results or len(results) <= 1:
        return results[:limit]

    backend = (settings.reranker_backend or "sentence-transformers").lower()

    # Serialize CPU-heavy reranker work so batch jobs (eval sweeps, LLM
    # scheduler jobs with rerank fallback) can't starve interactive
    # memory_search. Waiting here is correct — the results list is already
    # bounded (≤ overfetch) so the semaphore hold is short for any single
    # caller; it only clamps total concurrency across the process.
    sem = _get_rerank_semaphore()
    try:
        await asyncio.wait_for(
            sem.acquire(), timeout=settings.reranker_queue_timeout_s,
        )
    except asyncio.TimeoutError:
        # Too many reranks queued — skip rather than keep the caller
        # hanging. Better to return a merely-RRF-sorted list than to
        # block a chat response for 60s+. Caller sees results without
        # rerank_score, which the MCP layer passes through cleanly.
        logger.warning(
            "rerank queue timeout after %ss — falling back to RRF order",
            settings.reranker_queue_timeout_s,
        )
        return results[:limit]

    try:
        # Remote TEI sidecar — primary path when reranker_backend="http".
        # Keeps the backend image thin; model updates decouple from deploys.
        if backend == "http":
            try:
                return await _rerank_http(query, results, limit)
            except Exception:
                logger.exception(
                    "TEI reranker failed, falling through to sentence-transformers"
                )

        # In-process sentence-transformers path (legacy / fallback)
        if backend in ("sentence-transformers", "http") and _check_sentence_transformers():
            try:
                return await _rerank_sentence_transformers(query, results, limit)
            except Exception:
                logger.exception(
                    "sentence-transformers reranker failed, falling back to flashrank"
                )

        # Fallback / explicit choice
        if _check_flashrank():
            try:
                return await _rerank_flashrank(query, results, limit)
            except Exception:
                logger.exception("flashrank reranker failed, falling through to no-op")

        # Last-ditch no-op
        return results[:limit]
    finally:
        sem.release()
