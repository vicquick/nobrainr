"""Background scheduler for autonomous memory maintenance."""

import asyncio
import logging
from datetime import datetime

from nobrainr.config import settings
from nobrainr.db import queries

logger = logging.getLogger("nobrainr")

INITIAL_DELAY_SECONDS = 60

# Staggered initial delays for LLM jobs (seconds)
LLM_JOB_DELAYS = {
    "auto_summarize": 5 * 60,
    "insight_extraction": 15 * 60,
    "entity_enrichment": 25 * 60,
    "consolidation": 35 * 60,
    "synthesis": 45 * 60,
    "chatgpt_distill": 2 * 60,
    "contradiction_detection": 55 * 60,
    "cross_machine_insights": 60 * 60,
    "extraction_quality": 50 * 60,
}

# Per-job timeout for LLM operations
LLM_JOB_TIMEOUT = 10 * 60  # 10 minutes (increased for distillation)


class Scheduler:
    """Asyncio-based periodic task runner for memory maintenance jobs."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._llm_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Non-LLM jobs (existing)
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
            asyncio.create_task(
                self._run_periodic(
                    "memory_decay",
                    self._job_memory_decay,
                    settings.decay_interval_hours * 3600,
                )
            ),
        ]

        # LLM-powered jobs (import here to avoid circular imports at module level)
        from nobrainr import scheduler_jobs

        llm_jobs = [
            ("auto_summarize", scheduler_jobs.auto_summarize,
             settings.summarize_interval_hours * 3600),
            ("insight_extraction", scheduler_jobs.insight_extraction,
             settings.insight_extraction_interval_hours * 3600),
            ("entity_enrichment", scheduler_jobs.entity_enrichment,
             settings.entity_enrichment_interval_hours * 3600),
            ("consolidation", scheduler_jobs.consolidation,
             settings.consolidation_interval_hours * 3600),
            ("synthesis", scheduler_jobs.synthesis,
             settings.synthesis_interval_hours * 3600),
            ("chatgpt_distill", scheduler_jobs.chatgpt_distill,
             settings.chatgpt_distill_interval_hours * 3600),
            ("contradiction_detection", scheduler_jobs.contradiction_detection,
             settings.contradiction_interval_hours * 3600),
            ("cross_machine_insights", scheduler_jobs.cross_machine_insights,
             settings.cross_machine_interval_hours * 3600),
            ("extraction_quality", scheduler_jobs.extraction_quality,
             settings.quality_interval_hours * 3600),
        ]

        for name, job_func, interval in llm_jobs:
            self._tasks.append(
                asyncio.create_task(
                    self._run_periodic_llm(
                        name, job_func, interval, LLM_JOB_DELAYS[name],
                    )
                )
            )

        logger.info(
            "Scheduler started: maintenance=%.1fh, feedback=%.1fh, decay=%.1fh, "
            "summarize=%.1fh, insight=%.1fh, enrichment=%.1fh, consolidation=%.1fh, "
            "synthesis=%.1fh, contradiction=%.1fh, cross_machine=%.1fh, quality=%.1fh",
            settings.maintenance_interval_hours,
            settings.feedback_interval_hours,
            settings.decay_interval_hours,
            settings.summarize_interval_hours,
            settings.insight_extraction_interval_hours,
            settings.entity_enrichment_interval_hours,
            settings.consolidation_interval_hours,
            settings.synthesis_interval_hours,
            settings.contradiction_interval_hours,
            settings.cross_machine_interval_hours,
            settings.quality_interval_hours,
        )

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

    async def _run_periodic_llm(
        self, name: str, job, interval_seconds: float, initial_delay: float,
    ) -> None:
        """Run an LLM job periodically with lock, timeout, and staggered start."""
        await asyncio.sleep(initial_delay)
        while self._running:
            try:
                async with self._llm_lock:
                    logger.info("Running LLM job: %s", name)
                    result = await asyncio.wait_for(job(), timeout=LLM_JOB_TIMEOUT)
                    await queries.log_scheduler_event(name, result)
                    logger.info("LLM job '%s' completed: %s", name, result)
            except asyncio.TimeoutError:
                logger.warning("LLM job '%s' timed out after %ds", name, LLM_JOB_TIMEOUT)
                await queries.log_scheduler_event(name, {
                    "error": "timeout", "ran_at": datetime.now().isoformat(),
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LLM job '%s' failed", name)
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

    @staticmethod
    async def _job_memory_decay() -> dict:
        """Archive stale, low-value memories that are never accessed."""
        from nobrainr import scheduler_jobs
        return await scheduler_jobs.memory_decay()


# Module-level singleton
scheduler = Scheduler()
