"""Background scheduler for autonomous memory maintenance."""

import asyncio
import logging
from datetime import datetime

from nobrainr.config import settings
from nobrainr.db import queries

logger = logging.getLogger("nobrainr")

INITIAL_DELAY_SECONDS = 60

# Per-SQL-job initial delays (seconds). Prevents thundering-herd on boot
# where every periodic job fires inside the first minute and pegs CPU.
# LLM jobs already have LLM_JOB_DELAYS below; this is the analog for
# non-LLM SQL maintenance. Spread across 0-90min so a rebuild doesn't
# re-saturate the host while the service is still warming caches.
SQL_JOB_DELAYS: dict[str, float] = {
    # Fast + cheap — fire early so maintenance metrics are fresh
    "maintenance": 60,
    "feedback_integration": 120,
    "monitor_health": 30,
    "email_digest": 300,
    # Medium — one per 5-10 min through first half hour
    "memory_decay": 300,
    "auto_tier": 600,
    "entity_pruning": 900,
    "hub_dampening": 1200,
    "bridge_detection": 1500,
    # Slow / heavy — defer past the boot warm-up window
    # retrieval_eval runs 30 searches each with reranker, saturates CPU
    # for minutes. Keep out of the first 30min so interactive callers
    # have a predictable warm-up.
    "retrieval_eval": 3600,
}

# Staggered initial delays for LLM jobs (seconds).
# Designed for post-extraction graph optimization:
#   Phase 1: Clean the graph (dedup, connect, describe, cluster)
#   Phase 2: Score and consolidate memories
#   Phase 3: Generate new knowledge
#   Phase 4: External sources (web crawl, GitHub)
#   Phase 5: Meta (health, optimization)
LLM_JOB_DELAYS = {
    # === Phase 1: Graph cleanup (immediate — highest priority) ===
    "entity_merging": 30,           # deduplicate entities FIRST
    "quality_scoring": 1 * 60,      # score memories (parallel — lots of backlog)
    "cooccurrence_linking": 2 * 60, # build cross-branch connections
    "entity_enrichment": 4 * 60,    # describe entities for search
    "community_detection": 6 * 60,  # cluster the cleaned graph
    # === Phase 2: Memory quality ===
    "auto_summarize": 8 * 60,
    "extraction_quality": 10 * 60,
    "consolidation": 12 * 60,
    "fact_extraction": 3 * 60,      # high priority — build fact layer
    # === Phase 3: Knowledge growth ===
    # synthesis/contradiction/cross_machine run FIRST so they're not blocked
    # by chatgpt_distill's long lock hold time on first startup cycle
    "synthesis": 14 * 60,
    "contradiction_detection": 15 * 60,
    "cross_machine_insights": 16 * 60,
    "insight_extraction": 17 * 60,
    "chatgpt_distill": 18 * 60,  # runs AFTER knowledge-growth jobs have their first run
    # === Phase 4: External sources ===
    "knowledge_crawl": 24 * 60,
    "entity_web_research": 26 * 60,
    "freshness_recrawl": 28 * 60,
    "interest_expansion": 30 * 60,
    # === Phase 5: Meta ===
    "system_pulse": 32 * 60,
    "auto_optimize": 34 * 60,
    "github_sync": 36 * 60,
    # Retrieval-quality backfill — prioritise early after deploy while there's
    # a backlog, then self-throttles once the queue drains.
    "contextual_prefix_backfill": 5 * 60,
    # Tier-2 lesson tagger — classifies memories the SQL backfill missed.
    "lesson_classifier": 38 * 60,
    # Fact-augmented key expansion (LongMemEval pattern)
    "key_expansion": 7 * 60,
}

# Per-job timeout for LLM operations
LLM_JOB_TIMEOUT = 30 * 60  # 30 minutes for larger batch sizes

# Per-job timeout overrides for slow jobs (multi-pass distillation etc.)
LLM_JOB_TIMEOUT_OVERRIDES = {
    "chatgpt_distill": 60 * 60,   # 60 minutes — reduced batch_size=15, should finish faster
    "community_detection": 90 * 60,  # 42k entities + 50 community summaries = slow
}

# Per-row timeout for memory_write_queue worker. If the dedup/extraction hangs
# on a stuck llama-server (observed 2026-04-14 — GPU 97.7%, 0% util, worker
# blocked mid-LLM-call for 22h), this ceiling guarantees the row gets freed
# and the worker continues. P95 work duration is ~160s (see write_queue.py
# reset_stale_processing docstring), so 10 min is generous.
MEMORY_WRITE_WORKER_ROW_TIMEOUT = 10 * 60

# How often the orphan reaper runs while the worker is alive. Belt-and-braces
# alongside the per-row timeout: if the worker itself deadlocks (not just the
# LLM call inside it), this still recovers orphans without needing a restart.
STALE_PROCESSING_REAPER_INTERVAL = 5 * 60


class Scheduler:
    """Asyncio-based periodic task runner for memory maintenance jobs."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # Separate lifecycle flag for the memory_write_worker loop. Before
        # 2026-04-20 the worker used `self._running` and so was silently
        # stopped by /api/scheduler/pause, causing the queue to freeze the
        # moment anyone tried to "pause scheduler to free GPU for queue
        # drain". Keeping it independent means pause/resume only affects
        # background LLM maintenance jobs — queue writes keep flowing.
        self._write_queue_running = False
        # Soft pause for LLM maintenance jobs. Set True by /api/scheduler/pause
        # to stop entity_merging/quality_scoring/etc without cancelling tasks
        # (avoids the 30s tear-down + re-setup ping of full stop/start, and
        # keeps the write queue worker + reaper untouched).
        self._llm_jobs_paused = False
        self._llm_semaphore = asyncio.Semaphore(1)  # Hardcoded: single GPU, serialize LLM jobs

    @property
    def running(self) -> bool:
        return self._running

    def get_jobs(self) -> list[dict]:
        """Return all registered jobs with their configuration."""
        sql_jobs = [
            {"name": "maintenance", "interval_hours": settings.maintenance_interval_hours, "type": "sql"},
            {"name": "feedback_integration", "interval_hours": settings.feedback_interval_hours, "type": "sql"},
            {"name": "memory_decay", "interval_hours": settings.decay_interval_hours, "type": "sql"},
            {"name": "auto_tier", "interval_hours": 6.0, "type": "sql"},
            {"name": "entity_pruning", "interval_hours": 4.0, "type": "sql"},
            {"name": "hub_dampening", "interval_hours": 4.0, "type": "sql"},
            {"name": "bridge_detection", "interval_hours": 6.0, "type": "sql"},
            {"name": "retrieval_eval", "interval_hours": settings.retrieval_eval_interval_hours, "type": "sql"},
            {"name": "extraction_eval", "interval_hours": settings.extraction_eval_interval_hours, "type": "sql"},
            {"name": "monitor_health", "interval_hours": settings.monitoring_interval_hours, "type": "system"},
            {"name": "email_digest", "interval_hours": 24.0, "type": "system"},
        ]
        llm_jobs = [
            {"name": "chatgpt_distill", "interval_hours": settings.chatgpt_distill_interval_hours, "type": "llm"},
            {"name": "fact_extraction", "interval_hours": settings.fact_extraction_interval_hours, "type": "llm"},
            {"name": "auto_summarize", "interval_hours": settings.summarize_interval_hours, "type": "llm"},
            {"name": "insight_extraction", "interval_hours": settings.insight_extraction_interval_hours, "type": "llm"},
            {"name": "entity_enrichment", "interval_hours": settings.entity_enrichment_interval_hours, "type": "llm"},
            {"name": "entity_merging", "interval_hours": settings.entity_merging_interval_hours, "type": "llm"},
            {"name": "consolidation", "interval_hours": settings.consolidation_interval_hours, "type": "llm"},
            {"name": "synthesis", "interval_hours": settings.synthesis_interval_hours, "type": "llm"},
            {"name": "contradiction_detection", "interval_hours": settings.contradiction_interval_hours, "type": "llm"},
            {"name": "cross_machine_insights", "interval_hours": settings.cross_machine_interval_hours, "type": "llm"},
            {"name": "extraction_quality", "interval_hours": settings.quality_interval_hours, "type": "llm"},
            {"name": "knowledge_crawl", "interval_hours": settings.knowledge_crawl_interval_hours, "type": "llm"},
            {"name": "quality_scoring", "interval_hours": settings.quality_scoring_interval_hours, "type": "llm"},
            {"name": "key_expansion", "interval_hours": settings.key_expansion_interval_hours, "type": "llm"},
            {"name": "entity_web_research", "interval_hours": settings.entity_research_interval_hours, "type": "llm"},
            {"name": "freshness_recrawl", "interval_hours": settings.freshness_interval_hours, "type": "llm"},
            {"name": "interest_expansion", "interval_hours": settings.interest_expansion_interval_hours, "type": "llm"},
            {"name": "system_pulse", "interval_hours": settings.system_pulse_interval_hours, "type": "llm"},
            {"name": "auto_optimize", "interval_hours": settings.auto_optimize_interval_hours, "type": "llm"},
            {"name": "community_detection", "interval_hours": settings.community_detection_interval_hours, "type": "llm"},
            {"name": "cooccurrence_linking", "interval_hours": settings.cooccurrence_interval_hours, "type": "llm"},
            {"name": "github_sync", "interval_hours": settings.github_sync_interval_hours, "type": "llm"},
            {"name": "contextual_prefix_backfill", "interval_hours": 2.0, "type": "llm"},
            {"name": "lesson_classifier", "interval_hours": settings.lesson_classifier_interval_hours, "type": "llm"},
        ]
        return sql_jobs + llm_jobs

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._write_queue_running = True

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
            asyncio.create_task(
                self._run_periodic(
                    "auto_tier",
                    self._job_auto_tier,
                    6.0 * 3600,  # every 6 hours
                )
            ),
            asyncio.create_task(
                self._run_periodic(
                    "entity_pruning",
                    self._job_entity_pruning,
                    4.0 * 3600,  # every 4 hours — aggressive pruning post-extraction
                )
            ),
            asyncio.create_task(
                self._run_periodic(
                    "hub_dampening",
                    self._job_hub_dampening,
                    4.0 * 3600,  # every 4 hours — recompute entity specificity
                )
            ),
            asyncio.create_task(
                self._run_periodic(
                    "bridge_detection",
                    self._job_bridge_detection,
                    6.0 * 3600,  # every 6 hours — after community_detection
                )
            ),
            asyncio.create_task(
                self._run_periodic(
                    "retrieval_eval",
                    self._job_retrieval_eval,
                    settings.retrieval_eval_interval_hours * 3600,
                )
            ),
            asyncio.create_task(
                self._run_periodic(
                    "extraction_eval",
                    self._job_extraction_eval,
                    settings.extraction_eval_interval_hours * 3600,
                )
            ),
        ]

        # Monitoring jobs (non-LLM, subprocess-based)
        if settings.monitoring_enabled:
            from nobrainr import scheduler_jobs as sj

            self._tasks.append(
                asyncio.create_task(
                    self._run_periodic(
                        "monitor_health",
                        sj.monitor_health,
                        settings.monitoring_interval_hours * 3600,
                    )
                )
            )
            self._tasks.append(
                asyncio.create_task(
                    self._run_periodic(
                        "email_digest",
                        sj.send_email_digest,
                        24.0 * 3600,  # once per day
                    )
                )
            )

        # LLM-powered jobs (import here to avoid circular imports at module level)
        from nobrainr import scheduler_jobs

        llm_jobs = [
            ("auto_summarize", scheduler_jobs.auto_summarize,
             settings.summarize_interval_hours * 3600),
            ("insight_extraction", scheduler_jobs.insight_extraction,
             settings.insight_extraction_interval_hours * 3600),
            ("entity_enrichment", scheduler_jobs.entity_enrichment,
             settings.entity_enrichment_interval_hours * 3600),
            ("entity_merging", scheduler_jobs.entity_merging,
             settings.entity_merging_interval_hours * 3600),
            ("consolidation", scheduler_jobs.consolidation,
             settings.consolidation_interval_hours * 3600),
            ("synthesis", scheduler_jobs.synthesis,
             settings.synthesis_interval_hours * 3600),
            ("chatgpt_distill", scheduler_jobs.chatgpt_distill,
             settings.chatgpt_distill_interval_hours * 3600),
            # fact_extraction runs via dedicated _run_extraction_loop (not here)
            ("contradiction_detection", scheduler_jobs.contradiction_detection,
             settings.contradiction_interval_hours * 3600),
            ("cross_machine_insights", scheduler_jobs.cross_machine_insights,
             settings.cross_machine_interval_hours * 3600),
            ("extraction_quality", scheduler_jobs.extraction_quality,
             settings.quality_interval_hours * 3600),
            ("knowledge_crawl", scheduler_jobs.knowledge_crawl,
             settings.knowledge_crawl_interval_hours * 3600),
            ("quality_scoring", scheduler_jobs.quality_scoring,
             settings.quality_scoring_interval_hours * 3600),
            ("key_expansion", scheduler_jobs.key_expansion,
             settings.key_expansion_interval_hours * 3600),
            ("entity_web_research", scheduler_jobs.entity_web_research,
             settings.entity_research_interval_hours * 3600),
            ("freshness_recrawl", scheduler_jobs.freshness_recrawl,
             settings.freshness_interval_hours * 3600),
            ("interest_expansion", scheduler_jobs.interest_expansion,
             settings.interest_expansion_interval_hours * 3600),
            ("system_pulse", scheduler_jobs.system_pulse,
             settings.system_pulse_interval_hours * 3600),
            ("auto_optimize", scheduler_jobs.auto_optimize,
             settings.auto_optimize_interval_hours * 3600),
            ("community_detection", scheduler_jobs.community_detection,
             settings.community_detection_interval_hours * 3600),
            ("cooccurrence_linking", scheduler_jobs.cooccurrence_linking,
             settings.cooccurrence_interval_hours * 3600),
            ("github_sync", scheduler_jobs.github_sync,
             settings.github_sync_interval_hours * 3600),
            ("contextual_prefix_backfill", scheduler_jobs.contextual_prefix_backfill,
             2.0 * 3600),  # every 2h; each run processes 25 chunks, auto-idles when empty
            ("lesson_classifier", scheduler_jobs.lesson_classifier,
             settings.lesson_classifier_interval_hours * 3600),
        ]

        for name, job_func, interval in llm_jobs:
            self._tasks.append(
                asyncio.create_task(
                    self._run_periodic_llm(
                        name, job_func, interval, LLM_JOB_DELAYS[name],
                    )
                )
            )

        # Dedicated extraction loop — runs continuously during backlog, idles when clear
        self._tasks.append(asyncio.create_task(self._run_extraction_loop()))

        # Memory write queue worker — continuously drains memory_store
        # requests enqueued by the MCP tool. No initial delay: the queue
        # should drain on startup if there's a backlog from the previous
        # process. Shares the LLM semaphore with periodic jobs so it
        # never dogpiles the GPU.
        self._tasks.append(
            asyncio.create_task(self._memory_write_worker())
        )

        # Periodic orphan reaper. Defense-in-depth alongside the per-row
        # timeout inside the worker itself: if the worker's event loop
        # deadlocks (rather than just the LLM call inside it), this task
        # still unsticks the queue without requiring a container restart.
        self._tasks.append(
            asyncio.create_task(self._stale_processing_reaper())
        )

        sql_count = 7 + (2 if settings.monitoring_enabled else 0)
        logger.info(
            "Scheduler started with %d LLM jobs + %d SQL jobs. "
            "monitoring=%s (%.1fh), "
            "knowledge_crawl=%.1fh, entity_research=%.1fh, freshness=%.1fh, interest=%.1fh",
            len(llm_jobs),
            sql_count,
            "enabled" if settings.monitoring_enabled else "disabled",
            settings.monitoring_interval_hours,
            settings.knowledge_crawl_interval_hours,
            settings.entity_research_interval_hours,
            settings.freshness_interval_hours,
            settings.interest_expansion_interval_hours,
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
        """Run a job periodically with initial delay and exception resilience.

        The per-job delay comes from SQL_JOB_DELAYS so restarts don't fire
        every maintenance job in the first minute. Falls back to the
        global INITIAL_DELAY_SECONDS when a job isn't listed.
        """
        initial = SQL_JOB_DELAYS.get(name, INITIAL_DELAY_SECONDS)
        await asyncio.sleep(initial)
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
                try:
                    await queries.log_scheduler_event(name, {
                        "error": "exception", "ran_at": datetime.now().isoformat(),
                    })
                except Exception:
                    pass
            await asyncio.sleep(interval_seconds)

    async def _memory_write_worker(self) -> None:
        """Drain memory_write_queue forever.

        Claims rows FIFO via FOR UPDATE SKIP LOCKED, processes each through
        ``store_memory_with_extraction`` under the shared LLM semaphore, and
        updates the queue row to ``done`` or ``failed`` (with exp-backoff
        retry on transient LLM failures). When the queue is empty, awaits
        ``write_queue.wait_for_pending`` for up to 2s so new writes wake
        the worker immediately without burning CPU on a busy poll.

        Exception-resilient: any unhandled error restarts the loop after a
        2s grace period. CancelledError propagates so shutdown is clean.
        """
        # Import lazily so this module doesn't import services.memory at
        # module load (circular-import guard).
        from nobrainr.db import write_queue
        from nobrainr.services.memory import store_memory_with_extraction

        logger.info("memory_write_worker started — draining memory_write_queue")

        # Orphan reaper — Phase G (v6.9, 2026-04-12). When the previous
        # container was killed mid-task (Coolify rotation, OOM, manual stop)
        # any row claimed by that worker stayed in status='processing' with
        # no heartbeat. The claim loop below only looks at 'pending' rows,
        # so orphans would sit forever unless manually flipped back. Run a
        # one-shot reset at startup so the new worker picks them up.
        try:
            orphaned = await write_queue.reset_stale_processing(stale_minutes=10)
            if orphaned > 0:
                logger.info(
                    "memory_write_worker: recovered %d orphan row(s) from a previous worker crash",
                    orphaned,
                )
        except Exception:
            # A reaper failure should never block the worker from starting.
            logger.exception("memory_write_worker: orphan reset failed (continuing)")

        # Use independent lifecycle flag so /api/scheduler/pause doesn't
        # silently kill the queue worker. Pause should only affect the
        # background LLM maintenance jobs, not durable write processing.
        while self._write_queue_running:
            try:
                row = await write_queue.claim_next_pending()
                if row is None:
                    # Nothing to do — sleep until signalled or 2s elapses.
                    await write_queue.wait_for_pending(timeout=2.0)
                    continue

                queue_id = str(row["id"])
                logger.info(
                    "memory_write_worker: claimed %s (attempt %d/%d)",
                    queue_id, row["attempts"], row["max_attempts"],
                )

                # DO NOT share the scheduler LLM semaphore. llama-server
                # already serialises at HTTP (n_parallel=1) so the Python-
                # level semaphore was redundant — and if any scheduler LLM
                # job hung on its call while holding it, the write queue
                # worker blocked forever on semaphore acquire (observed
                # 2026-04-20: worker stopped after 1 row because entity_merging
                # held _llm_semaphore on a stuck call; queue froze at 124
                # pending for 30+ min). Dropping the semaphore lets worker
                # proceed independently; priority is already handled by the
                # live-vs-scheduler cooldown in extraction/llm.py.
                if True:
                    try:
                        result = await asyncio.wait_for(
                            store_memory_with_extraction(
                                content=row["content"],
                                summary=row["summary"],
                                tags=row["tags"],
                                category=row["category"],
                                source_type=row["source_type"] or "manual",
                                source_machine=row["source_machine"],
                                source_ref=row["source_ref"],
                                confidence=row["confidence"] if row["confidence"] is not None else 1.0,
                                metadata=row["metadata"],
                                skip_dedup=bool(row["skip_dedup"]),
                                contextual_prefix=row["contextual_prefix"],
                            ),
                            timeout=MEMORY_WRITE_WORKER_ROW_TIMEOUT,
                        )
                        memory_id = (
                            result.get("id")
                            or result.get("updated_id")
                            or result.get("new_id")
                        )
                        await write_queue.mark_done(
                            queue_id,
                            memory_id=memory_id,
                            result_status=result.get("status", "unknown"),
                        )
                        logger.info(
                            "memory_write_worker: %s → %s (memory_id=%s)",
                            queue_id, result.get("status"), memory_id,
                        )
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        # The LLM (dedup/extraction) hung — don't let the
                        # worker block the queue. Mark the row failed with
                        # retry so it goes back to pending with backoff.
                        logger.warning(
                            "memory_write_worker: %s timed out after %ds — marking failed+retry",
                            queue_id, MEMORY_WRITE_WORKER_ROW_TIMEOUT,
                        )
                        await write_queue.mark_failed(
                            queue_id,
                            error=f"worker row timeout ({MEMORY_WRITE_WORKER_ROW_TIMEOUT}s)",
                            retry=True,
                        )
                    except Exception as e:
                        logger.exception(
                            "memory_write_worker: processing %s failed", queue_id,
                        )
                        final_status = await write_queue.mark_failed(
                            queue_id, error=str(e), retry=True,
                        )
                        logger.info(
                            "memory_write_worker: %s → %s after failure",
                            queue_id, final_status,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "memory_write_worker: loop crashed, restarting in 2s",
                )
                await asyncio.sleep(2)

        logger.info("memory_write_worker stopped")

    async def _stale_processing_reaper(self) -> None:
        """Periodically reset memory_write_queue rows stuck in 'processing'.

        The worker itself carries a per-row timeout (see
        ``MEMORY_WRITE_WORKER_ROW_TIMEOUT``) so the common case of a hung
        LLM call is self-healing. This task exists for the rarer cases
        where the worker's own event loop stalls (GIL wedged on a sync
        call, asyncpg pool exhaustion, etc.) — situations where no
        in-process watchdog could fire. The startup reaper covered
        container-crash orphans; this covers alive-but-stuck orphans.
        """
        from nobrainr.db import write_queue

        # Skip the first tick — the worker's own startup reaper already
        # ran; no point resetting rows it just claimed.
        await asyncio.sleep(STALE_PROCESSING_REAPER_INTERVAL)

        while self._running:
            try:
                n = await write_queue.reset_stale_processing(stale_minutes=10)
                if n > 0:
                    logger.warning(
                        "stale_processing_reaper: freed %d orphan row(s) "
                        "(worker stalled mid-claim)", n,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stale_processing_reaper: tick failed")
            await asyncio.sleep(STALE_PROCESSING_REAPER_INTERVAL)

    async def _run_periodic_llm(
        self, name: str, job, interval_seconds: float, initial_delay: float,
    ) -> None:
        """Run an LLM job periodically with semaphore, timeout, and staggered start."""
        await asyncio.sleep(initial_delay)
        while self._running:
            # Soft-pause: when /api/scheduler/pause is called we skip
            # starting new LLM work but keep the loop alive so /resume
            # resumes instantly without re-creating tasks.
            if self._llm_jobs_paused:
                await asyncio.sleep(5)
                continue
            job_timeout = LLM_JOB_TIMEOUT_OVERRIDES.get(name, LLM_JOB_TIMEOUT)
            try:
                async with self._llm_semaphore:
                    logger.info("Running LLM job: %s", name)
                    result = await asyncio.wait_for(job(), timeout=job_timeout)
                    await queries.log_scheduler_event(name, result)
                    logger.info("LLM job '%s' completed: %s", name, result)
            except asyncio.TimeoutError:
                logger.warning("LLM job '%s' timed out after %ds", name, job_timeout)
                await queries.log_scheduler_event(name, {
                    "error": "timeout", "ran_at": datetime.now().isoformat(),
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LLM job '%s' failed", name)
            # Fairness yield: after releasing semaphore, give other jobs a chance
            # to acquire it before this job's next sleep→acquire cycle
            await asyncio.sleep(5)
            await asyncio.sleep(interval_seconds)

    async def _run_extraction_loop(self) -> None:
        """Dedicated extraction loop that runs continuously while backlog exists.

        Unlike _run_periodic_llm, this loop skips the interval sleep when there
        are pending memories — it grabs the GPU semaphore on every free cycle
        to clear the backlog as fast as possible. Falls back to a normal interval
        sleep when the queue is empty.
        """
        from nobrainr import scheduler_jobs
        await asyncio.sleep(LLM_JOB_DELAYS.get("fact_extraction", 60))
        idle_interval = settings.fact_extraction_interval_hours * 3600
        job_timeout = LLM_JOB_TIMEOUT_OVERRIDES.get("fact_extraction", LLM_JOB_TIMEOUT)
        while self._running:
            if self._llm_jobs_paused:
                await asyncio.sleep(5)
                continue
            try:
                async with self._llm_semaphore:
                    logger.info("Running LLM job: fact_extraction")
                    result = await asyncio.wait_for(
                        scheduler_jobs.fact_extraction(), timeout=job_timeout
                    )
                    await queries.log_scheduler_event("fact_extraction", result)
                    logger.info("LLM job 'fact_extraction' completed: %s", result)
            except asyncio.TimeoutError:
                logger.warning("LLM job 'fact_extraction' timed out after %ds", job_timeout)
                await queries.log_scheduler_event("fact_extraction", {
                    "error": "timeout", "ran_at": datetime.now().isoformat(),
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LLM job 'fact_extraction' failed")

            # Fairness yield: always give other jobs a chance to acquire the semaphore
            await asyncio.sleep(5)

            # Check if there's still work — if yes, loop immediately instead of sleeping
            try:
                pending = await queries.get_extraction_pending_count()
            except Exception:
                pending = 1  # assume work exists on error

            if pending > 0:
                logger.debug("fact_extraction: %d pending, looping immediately", pending)
                # No interval sleep — grab semaphore again ASAP
            else:
                logger.info("fact_extraction: backlog clear, sleeping %.0fs", idle_interval)
                await asyncio.sleep(idle_interval)

    @staticmethod
    async def _job_maintenance() -> dict:
        """Recompute importance + decay stability + analyze + apply retention
        (audit_log >7d, memory_versions keep-5)."""
        importance_count = await queries.recompute_importance()
        decay_count = await queries.decay_stability()
        retention = await queries.analyze_tables()
        return {
            "importance_recomputed": importance_count,
            "stability_decayed": decay_count,
            "audit_log_pruned": retention["audit_log_pruned"],
            "memory_versions_pruned": retention["memory_versions_pruned"],
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
    async def _job_retrieval_eval() -> dict:
        """Run the retrieval golden-set eval and record metrics.

        Weekly sweep of Recall@10 / MRR / nDCG@10 so we catch regressions
        when swapping models or tweaking the RRF / reranker pipeline.

        Gate: skip if eval_runs already has a row within one interval
        window. Without this, every container restart fires the sweep
        again, which saturates the reranker on CPU for minutes and
        starves live memory_search requests (observed 2026-04-19).
        """
        from nobrainr.services.eval_retrieval import run_retrieval_eval

        pool = await queries.get_pool()
        interval_s = settings.retrieval_eval_interval_hours * 3600
        # Leave a small buffer so the first tick after a legitimate
        # interval expiry still qualifies (clock drift / job-start delay).
        cutoff_s = interval_s * 0.95
        async with pool.acquire() as conn:
            recent = await conn.fetchval(
                """
                SELECT 1 FROM eval_runs
                WHERE ran_at > now() - ($1 || ' seconds')::interval
                LIMIT 1
                """,
                str(cutoff_s),
            )
        if recent:
            logger.info(
                "retrieval_eval: skipping — recent run within "
                "%.1fh window",
                cutoff_s / 3600,
            )
            return {
                "status": "skipped",
                "reason": "recent_run_exists",
                "ran_at": datetime.now().isoformat(),
            }

        try:
            result = await run_retrieval_eval(
                model_tag=settings.extraction_model,
                notes="scheduler",
            )
        except Exception as exc:
            logger.exception("retrieval_eval job failed")
            return {
                "status": "error",
                "error": str(exc),
                "ran_at": datetime.now().isoformat(),
            }
        result["ran_at"] = datetime.now().isoformat()
        return result

    @staticmethod
    async def _job_extraction_eval() -> dict:
        """A/B extraction eval (current model vs prior) on a sample of memories.

        Gate: skip if a run already exists within one interval window — the
        judge path is slow (~1-2min per sample) and frequent repeats have
        no value.
        """
        from nobrainr.services.eval_extraction import run_extraction_eval

        pool = await queries.get_pool()
        interval_s = settings.extraction_eval_interval_hours * 3600
        cutoff_s = interval_s * 0.95
        async with pool.acquire() as conn:
            recent = await conn.fetchval(
                """
                SELECT 1 FROM extraction_eval_runs
                WHERE ran_at > now() - ($1 || ' seconds')::interval
                LIMIT 1
                """,
                str(cutoff_s),
            )
        if recent:
            logger.info("extraction_eval: skipping — recent run within %.1fh window",
                        cutoff_s / 3600)
            return {"status": "skipped", "reason": "recent_run_exists",
                    "ran_at": datetime.now().isoformat()}
        try:
            result = await run_extraction_eval(
                candidate_model=settings.extraction_model,
                incumbent_model=settings.extraction_eval_incumbent_model,
                sample_size=settings.extraction_eval_sample_size,
            )
        except Exception as exc:
            logger.exception("extraction_eval job failed")
            return {"status": "error", "error": str(exc),
                    "ran_at": datetime.now().isoformat()}
        result["ran_at"] = datetime.now().isoformat()
        return result

    @staticmethod
    async def _job_memory_decay() -> dict:
        """Archive stale, low-value memories that are never accessed."""
        from nobrainr import scheduler_jobs
        return await scheduler_jobs.memory_decay()

    @staticmethod
    async def _job_auto_tier() -> dict:
        """Auto-assign memory tiers based on importance, access patterns, and quality."""
        counts = await queries.auto_tier_memories()
        counts["ran_at"] = datetime.now().isoformat()
        return counts

    @staticmethod
    async def _job_entity_pruning() -> dict:
        """Prune noise entities (<=1 memory link, older than 24h, no meaningful relations)."""
        from nobrainr import scheduler_jobs
        return await scheduler_jobs.entity_pruning()

    @staticmethod
    async def _job_hub_dampening() -> dict:
        """Compute IDF-like specificity scores for all entities."""
        from nobrainr import scheduler_jobs
        return await scheduler_jobs.hub_dampening()

    @staticmethod
    async def _job_bridge_detection() -> dict:
        """Find entities that bridge multiple communities."""
        from nobrainr import scheduler_jobs
        return await scheduler_jobs.bridge_detection()


# Module-level singleton
scheduler = Scheduler()
