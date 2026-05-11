"""Embedding client for Qwen3-Embedding (qwen3-embedding-cpu).

Hits the OpenAI-compatible `/v1/embeddings` endpoint on whatever backend
`settings.ollama_url` points at. The filename + module name are historical
— in production this routes to a `llama-server` (llama.cpp) process hosted
by `llama-swap`, not to Ollama. Both Ollama (>= 0.1.34) and llama-server
expose `/v1/embeddings` with the same request shape (`{model, input}`) and
the OpenAI response shape (`{data: [{embedding: [...], index: N}, ...]}`)
— so this client works against either backend without code branching.

Background: prior to 2026-05-11 this module posted to `/api/embed` (the
Ollama-native path). After the migration to llama-server the path returned
HTTP 404 from llama-server, silently failing 245+ memory writes over weeks
until detected. The endpoint switch is permanent — Ollama's OpenAI compat
layer has been stable since 2024.

The global _EMBED_SEM semaphore caps in-flight embed requests to 1 so that
background jobs (write_queue, scheduler backfills) cannot starve interactive
search. Search callers should use embed_text_with_timeout() which raises
EmbedTimeout on budget exhaustion — the search layer then falls back to
FTS+graph-only ranking rather than blocking for 50s+.
"""

import asyncio
import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

_client: httpx.AsyncClient | None = None

MAX_RETRIES = 3
RETRYABLE_STATUS = {404, 503, 502, 429}

# Reduce batch size: tuned for 20-core EPYC but we have i5-13500.
# i5-13500 needs ~7s per single embed; batch=8 stays within 60s Ollama timeout.
DEFAULT_BATCH_SIZE = 8

# Global semaphore: only 1 concurrent embed to Ollama.
# i5-13500 is single-threaded for GGML inference; concurrent requests queue
# inside Ollama and each waits for all previous. SEM(1) makes the queue visible
# on the Python side where we can apply timeouts and priorities.
_EMBED_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _EMBED_SEM
    if _EMBED_SEM is None:
        _EMBED_SEM = asyncio.Semaphore(1)
    return _EMBED_SEM


class EmbedTimeout(Exception):
    """Embed could not complete within the requested budget."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=90.0)
    return _client


async def _embed_with_retry(payload: dict) -> dict:
    """POST to /v1/embeddings with retry on transient errors.

    The `keep_alive` field in `payload` is Ollama-specific; llama-server
    silently ignores unknown fields, so the same payload shape works against
    both backends.
    """
    client = _get_client()
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post("/v1/embeddings", json=payload)
            if resp.status_code in RETRYABLE_STATUS:
                delay = 2 ** attempt
                logger.warning(
                    "Embed returned %d, retrying in %ds (attempt %d/%d)",
                    resp.status_code, delay, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp,
                )
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            delay = 2 ** attempt
            logger.warning(
                "Embed connection error: %s, retrying in %ds (attempt %d/%d)",
                exc, delay, attempt + 1, MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            last_exc = exc

    raise last_exc or RuntimeError("Embedding failed after retries")


def _extract_vectors(data: dict) -> list[list[float]]:
    """Pull the embedding vectors out of an OpenAI-shape response.

    Returns the list of vectors in the same order as the input list,
    using `index` if present, otherwise positional order. The OpenAI
    spec guarantees `data` is ordered, but the index field exists so
    that re-ordered responses (rare with batching) still resolve right.
    """
    items = data.get("data") or []
    if not items:
        return []
    if all("index" in item for item in items):
        items = sorted(items, key=lambda x: x["index"])
    return [item["embedding"] for item in items]


async def embed_text(text: str) -> list[float]:
    """Generate embedding for a single text string (background-safe, no timeout)."""
    sem = _get_sem()
    async with sem:
        data = await _embed_with_retry(
            {"model": settings.embedding_model, "input": text, "keep_alive": "24h"},
        )
    vectors = _extract_vectors(data)
    if not vectors:
        raise RuntimeError("Embed returned no vectors")
    return vectors[0]


# Query embedding cache: key = sha1(model + lower(strip(query))), value = (vec, ts).
# 7s baseline embed is the search latency floor on CPU. Cache repeat queries
# (common patterns like "llama config", "qwen quant") at <1ms instead. 1000-
# entry cap, 24h TTL. Hit ratio is high in normal use because users iterate
# on the same query while exploring results.
_QCACHE: dict[str, tuple[list[float], float]] = {}
_QCACHE_MAX = 1000
_QCACHE_TTL_S = 24 * 3600


def _qcache_key(text: str) -> str:
    import hashlib
    norm = " ".join((text or "").lower().split())
    h = hashlib.sha1()
    h.update(settings.embedding_model.encode())
    h.update(b"\x00")
    h.update(norm.encode("utf-8"))
    return h.hexdigest()


def _qcache_get(text: str) -> list[float] | None:
    import time
    key = _qcache_key(text)
    rec = _QCACHE.get(key)
    if rec is None:
        return None
    vec, ts = rec
    if time.time() - ts > _QCACHE_TTL_S:
        _QCACHE.pop(key, None)
        return None
    return vec


def _qcache_put(text: str, vec: list[float]) -> None:
    import time
    if len(_QCACHE) >= _QCACHE_MAX:
        # Drop oldest entry (simple LRU approximation by sorted ts)
        oldest = min(_QCACHE.items(), key=lambda kv: kv[1][1])[0]
        _QCACHE.pop(oldest, None)
    _QCACHE[_qcache_key(text)] = (vec, time.time())


async def embed_text_with_timeout(text: str, timeout_s: float = 15.0) -> list[float]:
    """Embed with a budget. Raises EmbedTimeout if semaphore wait + inference > timeout_s.

    Hits the in-process query cache first — repeat queries return in <1ms
    instead of 7s. Cache keyed by (model, normalized_query), 1000-entry cap,
    24h TTL. See _QCACHE module-level state.

    Use this in the interactive search path so a flooded Ollama queue causes
    graceful FTS fallback rather than a 50s hang.
    """
    cached = _qcache_get(text)
    if cached is not None:
        return cached

    sem = _get_sem()
    try:
        # Wait for semaphore + run inference, all within timeout_s.
        async def _do():
            async with sem:
                data = await _embed_with_retry(
                    {"model": settings.embedding_model, "input": text, "keep_alive": "24h"},
                )
            vectors = _extract_vectors(data)
            if not vectors:
                raise RuntimeError("Embed returned no vectors")
            return vectors[0]

        vec = await asyncio.wait_for(_do(), timeout=timeout_s)
        _qcache_put(text, vec)
        return vec
    except asyncio.TimeoutError as e:
        raise EmbedTimeout(f"embed exceeded {timeout_s}s budget") from e


async def embed_batch(
    texts: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    """Generate embeddings for multiple texts (background-safe, serialised)."""
    sem = _get_sem()
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        async with sem:
            data = await _embed_with_retry(
                {"model": settings.embedding_model, "input": batch, "keep_alive": "24h"},
            )
        all_embeddings.extend(_extract_vectors(data))
    return all_embeddings


async def check_model() -> bool:
    """Check if the embedding model is available on the inference backend.

    Hits the OpenAI-compatible `/v1/models` endpoint; the response shape is
    `{"data": [{"id": "<label>", ...}, ...]}`. Returns True if any model
    label starts with the configured embedding model name OR matches any
    of the registered aliases (handles llama-swap label-vs-filename drift
    that bit us on 2026-04-08).
    """
    try:
        client = _get_client()
        resp = await client.get("/v1/models")
        resp.raise_for_status()
        models = resp.json().get("data", [])
        ids = [m.get("id", "") for m in models]
        targets = {settings.embedding_model, *settings.embedding_model_aliases}
        return any(any(mid.startswith(t) for t in targets) for mid in ids)
    except Exception:
        return False
