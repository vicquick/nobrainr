"""PR A (2026-08-14): brief gains failed-fix + procedural lanes, eval gains
token-cost accounting, observability gains retrieval concentration.
SQL-invariant + config-shape tests in the house mock style."""

from __future__ import annotations

import inspect


def test_brief_errors_lane_sql_invariants():
    from nobrainr.dashboard import api

    src = inspect.getsource(api.api_brief)
    # Failed-fix lane: error-pattern tag filter, cold included, high sim bar
    assert '"error-pattern"' in src or "'error-pattern'" in src
    assert "include_cold=True" in src
    assert ">= 0.6" in src
    # Procedural lane: active + unexpired only, bounded output
    assert "procedural_memories" in src
    assert "WHERE active" in src
    assert "expires_at IS NULL OR expires_at > now()" in src
    # Both lanes must never break the brief
    assert src.count("except Exception") >= 4
    # Response carries the new sections
    assert '"errors": errors' in src and '"procedures": procedures' in src


def test_eval_reports_avg_result_tokens():
    from nobrainr.services import eval_retrieval

    src = inspect.getsource(eval_retrieval.run_retrieval_eval)
    assert "result_tokens_total" in src
    assert '"avg_result_tokens"' in src
    # chars/4 estimate over content+summary of returned results
    assert "// 4" in src


def test_observability_reports_concentration():
    from nobrainr import scheduler_jobs

    src = inspect.getsource(scheduler_jobs.memory_observability)
    assert "top20_share_7d" in src
    assert "unnest(top_ids)" in src
    assert '"concentration"' in src


def test_card_builder_excludes_alert_streams():
    """2026-08-14 garbage-card incident: alert-spam entities must neither
    qualify for nor source cards."""
    import inspect

    from nobrainr import scheduler_jobs

    src = inspect.getsource(scheduler_jobs.card_builder)
    assert src.count("category NOT IN ('monitoring', 'session-log')") == 2


def test_brief_colleagues_lane_invariants():
    """Colleagues lane (2026-08-15): related active-agent tasks in the
    brief — fresh presence only, scored, fail-open."""
    import inspect

    from nobrainr.dashboard import api

    src = inspect.getsource(api.api_brief)
    assert "agent_presence" in src
    assert "interval '30 minutes'" in src
    assert '"colleagues": colleagues' in src
    assert src.count("except Exception") >= 5
