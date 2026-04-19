"""In-memory latency + depth metrics for the health endpoint.

Keep it tiny. No prometheus, no SQL. Each metric is a fixed-capacity
ring buffer of (timestamp, value) pairs so we can compute quantiles
over the last N seconds without unbounded growth.

Used by /api/health/detailed so operators can see at a glance whether
search has degraded, whether a batch job is chewing the reranker,
or whether the write queue is backing up.
"""

from __future__ import annotations

import statistics
from collections import deque
from time import monotonic


_MAX_POINTS = 2048  # ~30min at 1 req/sec — plenty for rolling window

_search_latency_ms: deque[tuple[float, float]] = deque(maxlen=_MAX_POINTS)
_rerank_queue_samples: deque[tuple[float, int]] = deque(maxlen=_MAX_POINTS)


def record_search_latency(ms: float) -> None:
    _search_latency_ms.append((monotonic(), float(ms)))


def record_rerank_queue_depth(depth: int) -> None:
    _rerank_queue_samples.append((monotonic(), int(depth)))


def _window(samples: deque, window_s: float) -> list:
    if not samples:
        return []
    now = monotonic()
    cutoff = now - window_s
    return [v for (t, v) in samples if t >= cutoff]


def search_latency_stats(window_s: float = 60.0) -> dict:
    """Return p50/p95/p99 + count for search latency over the window."""
    pts = _window(_search_latency_ms, window_s)
    if not pts:
        return {"count": 0, "window_s": window_s}
    pts_sorted = sorted(pts)

    def q(frac: float) -> float:
        if not pts_sorted:
            return 0.0
        idx = min(len(pts_sorted) - 1, int(len(pts_sorted) * frac))
        return pts_sorted[idx]

    return {
        "count": len(pts_sorted),
        "window_s": window_s,
        "p50_ms": round(q(0.50), 1),
        "p95_ms": round(q(0.95), 1),
        "p99_ms": round(q(0.99), 1),
        "mean_ms": round(statistics.fmean(pts_sorted), 1),
        "max_ms": round(max(pts_sorted), 1),
    }


def rerank_queue_stats(window_s: float = 60.0) -> dict:
    pts = _window(_rerank_queue_samples, window_s)
    if not pts:
        return {"count": 0, "window_s": window_s}
    return {
        "count": len(pts),
        "window_s": window_s,
        "current": pts[-1] if pts else 0,
        "max": max(pts),
        "mean": round(statistics.fmean(pts), 2),
    }
