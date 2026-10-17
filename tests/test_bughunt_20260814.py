"""2026-08-14 bug-hunt fixes: _jsonb Decimal hardening, reranker_eval
persist split, orphan scheduler_runs cleanup at boot."""

from __future__ import annotations

import inspect
from datetime import datetime
from decimal import Decimal
from uuid import uuid4


def test_jsonb_survives_decimal_datetime_uuid():
    from nobrainr.db.queries import _jsonb

    out = _jsonb({"share": Decimal("0.1234"), "at": datetime(2026, 8, 14),
                  "id": uuid4(), "n": 3})
    assert '"0.1234"' in out and '"n": 3' in out


def test_reranker_eval_persist_is_split_statements():
    from nobrainr import scheduler_jobs

    src = inspect.getsource(scheduler_jobs.reranker_eval)
    # The multi-command prepared statement (CREATE + INSERT in one call
    # with a parameter) failed on every run since 07-21. Then the split
    # version still targeted extraction_eval_runs, whose schema.py columns
    # don't match (2026-10-17). Now: one INSERT into its own table.
    execs = src.split("await conn.execute(")[1:]
    assert len(execs) == 1
    assert "INSERT INTO reranker_eval_runs" in execs[0]
    assert "extraction_eval_runs" not in execs[0]
    assert "CREATE TABLE" not in execs[0]


def test_scheduler_boot_closes_orphan_running_rows():
    from nobrainr import scheduler

    src = inspect.getsource(scheduler.Scheduler.start)
    assert "SET status = 'killed'" in src
    assert "WHERE status = 'running'" in src
