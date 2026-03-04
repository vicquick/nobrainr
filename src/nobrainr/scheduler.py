"""Background scheduler for autonomous memory maintenance."""

import asyncio
import logging
from datetime import datetime

from nobrainr.config import settings
from nobrainr.db import queries

logger = logging.getLogger("nobrainr")

INITIAL_DELAY_SECONDS = 60


class Scheduler:
    """Asyncio-based periodic task runner for memory maintenance jobs."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(
                self._run_periodic(
                    "maintenance",
                    self._job_maintenance,
                    settings.maintenance_interval_hours * 3600,
                )
            ),
            asyncio.create_task(
                self._run_periodic(
                    "feedback_integration",
                    self._job_feedback_integration,
                    settings.feedback_interval_hours * 3600,
                )
            ),
        ]
        logger.info("Scheduler started (maintenance=%.1fh, feedback=%.1fh)",
                     settings.maintenance_interval_hours,
                     settings.feedback_interval_hours)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Scheduler stopped")

    async def _run_periodic(self, name: str, job, interval_seconds: float) -> None:
        """Run a job periodically with initial delay and exception resilience."""
        await asyncio.sleep(INITIAL_DELAY_SECONDS)
        while self._running:
            try:
                logger.info("Running scheduled job: %s", name)
                result = await job()
                await queries.log_scheduler_event(name, result)
                logger.info("Scheduled job '%s' completed: %s", name, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled job '%s' failed", name)
            await asyncio.sleep(interval_seconds)

    @staticmethod
    async def _job_maintenance() -> dict:
        """Recompute importance scores + decay stability for stale memories."""
        importance_count = await queries.recompute_importance()
        decay_count = await queries.decay_stability()
        return {
            "importance_recomputed": importance_count,
            "stability_decayed": decay_count,
            "ran_at": datetime.now().isoformat(),
        }

    @staticmethod
    async def _job_feedback_integration() -> dict:
        """Adjust importance based on memory_outcomes feedback."""
        updated = await queries.integrate_feedback_scores()
        return {
            "feedback_adjusted": updated,
            "ran_at": datetime.now().isoformat(),
        }


# Module-level singleton
scheduler = Scheduler()
