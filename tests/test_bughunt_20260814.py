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
    # with a parameter) failed on every run since 07-21. Each execute
    # must now carry exactly one command.
    creates = [seg for seg in src.split("await conn.execute(")
               if "CREATE TABLE IF NOT EXISTS extraction_eval_runs" in seg]
    assert len(creates) == 1
    assert "INSERT INTO" not in creates[0].split('"""')[1]


def test_scheduler_boot_closes_orphan_running_rows():
    from nobrainr import scheduler

    src = inspect.getsource(scheduler.Scheduler.start)
    assert "SET status = 'killed'" in src
    assert "WHERE status = 'running'" in src
