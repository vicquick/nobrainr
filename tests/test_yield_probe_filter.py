"""GPU-yield probe filter (2026-08-15): 1-token heartbeats must not park
the 27b — only real conversations do."""

from __future__ import annotations

import inspect


def test_probe_sized_requests_do_not_arm_the_park():
    from nobrainr.extraction import llm

    src = inspect.getsource(llm._live_model_active)
    assert "output_tokens" in src and "<= 2" in src
    assert "input_tokens" in src and "<= 8" in src
    # the probe check must run BEFORE the recency comparison
    assert src.index("output_tokens") < src.index("ts >= cutoff")
