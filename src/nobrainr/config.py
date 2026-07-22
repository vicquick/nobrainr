"""Configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "NOBRAINR_"}

    # PostgreSQL
    database_url: str = "postgresql://nobrainr:nobrainr@localhost:5432/nobrainr"

    # LLM inference (llama-server for GPU, Ollama for embeddings)
    llm_server_url: str = "http://llama-server:8080"
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "qwen3-embedding-cpu"
    # Additional labels that refer to bit-identical vectors. The search
    # safeguard matches ANY of these so runtime-only tag drift (cpu vs gpu,
    # :0.6b vs no suffix, :latest) never silently hides 95% of memories
    # again the way it did on 2026-04-08.
    embedding_model_aliases: list[str] = [
        "qwen3-embedding-cpu",
        "qwen3-embedding:0.6b",
        "qwen3-embedding",
        "qwen3-embedding:latest",
        "qwen3-embedding-cpu:latest",
    ]
    embedding_dimensions: int = 1024

    # GPU yield (2026-06-13). llama-swap runs one exclusive "chat" slot:
    # qwen3-8b (live apps: bimavo wizard, MCP tools) XOR qwen3.6-27b
    # (scheduler: distill, extraction, scoring). A scheduler call while a
    # live app is mid-conversation on the 8b evicts it and costs two ~35s
    # swaps per exchange. Instead, scheduler calls poll llama-swap
    # /api/metrics and park while any model listed here served a request
    # within recent_s. Request-recency (not residency) is the signal:
    # residency flips back the moment one scheduler call slips through,
    # while recency keeps the park alive across the gaps between user
    # exchanges. max_wait caps the park so background jobs degrade to
    # slow instead of starving forever under constant live load. Empty
    # list disables the check (plain llama-server has no /api/metrics
    # and is also handled by the try/except). Names must match the
    # canonical llama-swap model keys, not aliases.
    gpu_yield_models: list[str] = ["qwen3-8b"]
    gpu_yield_recent_s: float = 600.0
    gpu_yield_max_wait_s: float = 900.0
    gpu_yield_poll_s: float = 15.0

    # MCP Server
    host: str = "0.0.0.0"
    port: int = 8420

    # Search defaults
    default_search_limit: int = 10
    default_similarity_threshold: float = 0.3

    # Crawl4AI
    crawl4ai_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: str = ""

    # Brave Search API (web discovery — Brave discovers URLs, crawl4ai extracts)
    brave_api_key: str = ""
    brave_search_url: str = "https://api.search.brave.com/res/v1/web/search"
    # Monthly query budget mirroring the Brave dashboard free-tier cap
    # ($5 credits ≈ 1000 queries). Past this, web_search returns a clean
    # "quota exhausted" error instead of Brave's 401/429. 0 = no block
    # (usage is still counted for visibility).
    brave_monthly_query_cap: int = 1000

    # Speaches (OpenAI-compatible whisper API)
    speaches_url: str = "http://speaches:8000"
    speaches_model: str = "whisper-large-v3"
    speaches_tts_model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX-fp16"
    speaches_tts_voice: str = "af_heart"  # Kokoro default voice

    # Chunked ingestion
    chunk_max_chars: int = 6000  # max chars per chunk
    chunk_overlap_chars: int = 500  # overlap between consecutive chunks
    chunk_threshold: int = 8000  # content above this length gets chunked
    chunk_context_window: int = 1  # fetch N adjacent chunks around search hits
    contextual_embeddings_enabled: bool = True  # prepend LLM-generated context to chunks before embedding

    # Reranking.
    #  - "sentence-transformers" (default): BAAI/bge-reranker-v2-m3, 100+ languages,
    #    cross-encoder, ~560MB, CPU inference ~200ms for 150 docs. Best quality.
    #  - "flashrank": ONNX, English-only MiniLM, ~100ms for 30 docs. Fallback for
    #    environments that can't install torch.
    reranker_enabled: bool = True
    # Backend preference order. "http" hits a dedicated TEI sidecar
    # (ghcr.io/huggingface/text-embeddings-inference), keeps the backend
    # container thin and model updates independent. Falls through to
    # local sentence-transformers (then flashrank) on HTTP failure so
    # the reranker stays available if the sidecar is mid-deploy.
    reranker_backend: str = "http"
    # When the http sidecar fails, may the process lazy-load the 2GB
    # in-process sentence-transformers CrossEncoder as a fallback?
    # Default OFF (2026-07-06): one transient HTTP timeout used to pull
    # 2GB into WHATEVER process was searching — the standalone eval
    # runner did exactly that and died when host swap was exhausted.
    # Degrading to RRF order (tier C) is the designed graceful path;
    # opt back in per-process via NOBRAINR_RERANKER_INPROCESS_FALLBACK=1.
    # An explicit reranker_backend="sentence-transformers" still works.
    reranker_inprocess_fallback: bool = False
    # URL of the TEI sidecar. Resolved in-cluster via Docker DNS on the
    # `mcp` network (alias `reranker`). Override with NOBRAINR_RERANKER_URL
    # if routing differs per deploy.
    reranker_url: str = "http://reranker:80"
    reranker_http_timeout_s: float = 30.0
    # 2026-05-06 default swap: Qwen3-Reranker-0.6B (Apr 2026) beats
    # BGE-reranker-v2-m3 on every MTEB benchmark category — 65.80 vs
    # 57.03 on MTEB-R, 73.42 vs 41.38 on MTEB-Code. Drop-in compatible
    # via sentence-transformers v3.4+. Override via NOBRAINR_RERANKER_MODEL
    # env var to roll back if needed.
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    reranker_fallback_model: str = "ms-marco-MiniLM-L-12-v2"  # flashrank name
    # Sigmoid-normalise reranker scores to [0,1] so threshold semantics
    # (auto_negative_low_rerank_threshold) remain stable across model
    # swaps. Both BGE and Qwen3 emit raw logits in similar ranges.
    reranker_apply_sigmoid: bool = True
    # Device for sentence-transformers cross-encoder. 'cuda' is 5x faster
    # (200-500ms for 8 docs vs 1-2s on CPU) but competes with llama-server
    # for VRAM. _get_st_reranker auto-falls-back to CPU if cuda load fails.
    reranker_device: str = "cuda"
    # Cap concurrent cross-encoder runs. i5-13500 = 20 threads but a single
    # bge-reranker-v2-m3 call on 150 docs spins 10+ cores. Set to 2 so a
    # batch caller can't lock out interactive search. See incident
    # 2026-04-19: 30-query eval sweep pegged CPU at 1063% for minutes.
    reranker_concurrency: int = 2
    # Hard cap on how long a rerank can wait for the semaphore before it
    # gives up and returns pre-rerank order. Keeps interactive search
    # responsive under any batch load.
    reranker_queue_timeout_s: float = 10.0
    # Max candidates sent to the cross-encoder. 8 was the CPU-era cap
    # (BGE on CPU Candle = 1-2s/doc). Since llama-swap serves the reranker
    # GGUF on GPU (measured 2026-07-05: 50 docs in 0.87s via /v1/rerank),
    # a wide rerank is affordable again — this is the Anthropic recipe's
    # main quality lever and directly targets the recall@10 regression
    # (0.70→0.51 as corpus grew 48k→72k). The search_rerank_budget_frac
    # guard still degrades to RRF order if the GPU is contended.
    reranker_max_candidates: int = 50
    # RRF candidate pool: how many DB results to retrieve per branch before
    # fusion. 6× gives 300 candidates for limit=50 — ample diversity for RRF
    # without forcing the HNSW index to traverse thousands of nodes.
    # Old value was 15× (Anthropic GPU recipe), which caused 13-20s searches
    # on the 56K corpus because inner_overfetch = 15*limit*3 = 2250 HNSW nodes.
    # Raise to 10-15 only if TEI reranker gets a GPU slot and max_candidates > 50.
    overfetch_mult: int = 6

    # Reranker skip-when-dominant (2026-04-19). If top-1 RRF score is
    # ≥ rerank_skip_dominance_ratio × top-2 score, skip the reranker —
    # the cross-encoder can't meaningfully reorder when RRF already has
    # a clear winner. Saves CPU on easy queries so the reranker is free
    # when it actually matters. 1.8× is empirically dominant.
    rerank_skip_when_dominant: bool = True
    # Raised from 1.8 → 2.5 (2026-04-19): 1.8 caused some rank-1 hits
    # that the reranker would have promoted from rank-2 to lose out. A
    # 2.5× gap means RRF had a truly dominant winner and the reranker
    # really can't change it.
    rerank_skip_dominance_ratio: float = 2.5

    # Graph-aware 4th RRF branch (2026-04-19, HippoRAG-lite). Fuzzy-matches
    # query terms against entities.canonical_name via pg_trgm and lifts
    # memories that are linked to matched entities in the knowledge graph.
    # Captures most of the associative-memory lift (HippoRAG 2 reported +7%
    # on multi-hop tasks) without running full Personalized PageRank.
    graph_branch_enabled: bool = True
    # Short queries match too many trigram candidates and slow the branch
    # down for no quality gain. Skip if query is below this.
    graph_branch_min_query_chars: int = 8
    # pg_trgm similarity threshold for entity fuzzy match. 0.5 empirically
    # (2026-04-19 eval): 0.3 was too loose and pulled semi-matching
    # entities that RRF-boosted irrelevant memories past the correct
    # rank-1 hits, causing a -6pp Recall@10 regression. 0.5 requires real
    # word-level overlap before the graph branch fires.
    graph_branch_trigram_threshold: float = 0.5
    # Max entities to consider after trigram match.
    graph_branch_max_entities: int = 10
    # Graph branch contributes at this RRF weight — kept below 0.5 so
    # strong vector + FTS hits keep ownership of the top ranks; graph
    # branch is additive signal for multi-hop, not a prior.
    graph_branch_rrf_weight: float = 0.35

    # ── HippoRAG 2 PPR layer (2026-05-03) ─────────────────────────
    # When the graph branch fires and finds at least `ppr_min_seeds` seed
    # entities, run Personalized PageRank from those seeds across the
    # entity_relations graph. Returns top-K associatively-related entities
    # which are folded into the same graph_score memory join. HippoRAG 2
    # paper (arxiv 2502.14802): +7% on associative-memory queries.
    ppr_enabled: bool = True
    ppr_min_seeds: int = 1            # need at least 1 seed to walk from
    ppr_alpha: float = 0.85           # HippoRAG 2 default — restart prob 0.15
    ppr_iterations: int = 10          # power-iteration count
    ppr_top_k: int = 200              # max entities to keep after expansion
    # Weight applied to PPR-walked entity scores relative to direct seeds.
    # PPR scores are normalised to [0,1]; multiplying by avg-seed-idf scales
    # them to be commensurate with seed weights without ever exceeding them.
    ppr_score_weight: float = 0.6

    # ── Community-summary branch (GraphRAG-style, 2026-05-04) ─────
    # Match query against LLM-generated summaries of Leiden communities.
    # Answers "what's our debugging philosophy?" / "summarize my X" type
    # global queries that don't map cleanly to a single memory but are
    # well-described by an aggregate community summary. Top-K matched
    # communities yield their representative memories.
    community_branch_enabled: bool = True
    community_branch_top_k_communities: int = 5
    # RRF weight kept low — community matches are global-context signal,
    # additive to specific RRF top-1 hits, not a prior.
    community_branch_rrf_weight: float = 0.25

    # End-to-end deadline for memory_search. Above this we short-circuit
    # expensive stages (rerank, related_memories, chunk_context) and return
    # whatever we have with a `quality_tier` tag so the caller sees it
    # got a degraded result instead of a hang. 20s is generous for the
    # happy path but ruthless under load.
    search_hard_timeout_s: float = 20.0
    # Skip the reranker if we've already burned this fraction of the
    # budget on earlier stages. 0.75 = if ≥15s used out of 20s, skip.
    search_rerank_budget_frac: float = 0.75

    # Security
    cors_origins: list[str] = ["http://localhost:8420"]
    max_content_length: int = 50000  # 50KB max memory content

    # Extraction (knowledge graph)
    extraction_model: str = "qwen3.6:35b"
    extraction_enabled: bool = True

    # Chat (RAG)
    chat_model: str = ""  # defaults to extraction_model
    chat_max_context_memories: int = 5  # reduced from 15→10→5 for fast chat (~10s prefill)
    chat_max_source_memories: int = 15  # reduced from 50 — less entity noise in UI
    chat_max_message_length: int = 2000
    chat_max_history_length: int = 20
    chat_enabled: bool = True

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_llm_concurrency: int = 5  # max concurrent LLM jobs (leave slots for live requests)
    scheduler_inter_request_delay: float = 1.0  # seconds between LLM calls in batch jobs
    maintenance_interval_hours: float = 6.0
    feedback_interval_hours: float = 12.0

    # Machine identifier for scheduler-created memories (defaults to hostname)
    source_machine: str = ""

    # LLM scheduler jobs — BALANCED MODE: fact extraction + graph evolution
    scheduler_llm_model: str = "qwen3.6:35b"
    summarize_interval_hours: float = 1.0
    summarize_batch_size: int = 20
    consolidation_interval_hours: float = 2.0
    consolidation_batch_size: int = 10
    synthesis_interval_hours: float = 3.0
    synthesis_batch_size: int = 5
    entity_enrichment_interval_hours: float = 2.0
    entity_enrichment_batch_size: int = 20
    entity_merging_interval_hours: float = 2.0
    entity_merging_batch_size: int = 15
    insight_extraction_interval_hours: float = 1.0
    insight_extraction_batch_size: int = 30
    chatgpt_distill_interval_hours: float = 2.0  # 2h — backlog done, free GPU for fact extraction
    # 2026-05-13 cut 15→10: with batch 15 every other run was hitting the 120m
    # timeout (per-call work scales linearly, GPU contention from concurrent
    # quality_scoring tripled the worst-case). Batch 10 keeps run time well
    # under the (now 180m) ceiling with headroom for one slow conversation.
    chatgpt_distill_batch_size: int = 4  # final 43 long-tail convos (avg 269 msgs) need a small batch to finish inside the run window
    chatgpt_distill_concurrency: int = 1
    chatgpt_distill_model: str = "qwen3.6:35b"
    # Fact extraction (NEW — Mem0-style atomic facts)
    fact_extraction_interval_hours: float = 0.05  # 3min — aggressive during backfill
    # Cut 200 → 20 on 2026-04-20: a 200-memory batch holds the LLM semaphore
    # for 10-30min, starving quality_scoring + all other LLM jobs (observed:
    # quality_scoring last_run was 6h old with 33K unscored backlog). With
    # batch=20 the extraction loop yields every ~2min, letting sibling jobs
    # get their turn. Total throughput similar since the fairness yield
    # sleeps only 5s between batches when backlog exists.
    fact_extraction_batch_size: int = 200
    # Memory decay
    decay_interval_hours: float = 24.0
    decay_batch_size: int = 50
    # Contradiction detection
    contradiction_interval_hours: float = 4.0
    contradiction_batch_size: int = 10
    # Cross-machine insights
    cross_machine_interval_hours: float = 6.0
    cross_machine_batch_size: int = 5
    # Extraction quality
    quality_interval_hours: float = 4.0
    quality_batch_size: int = 20
    # Memory quality scoring (LLM-assessed) — packed 8-per-call since 2026-06-06,
    # so a 120 batch is 15 LLM calls (~7x throughput vs per-memory calls)
    quality_scoring_interval_hours: float = 0.25
    quality_scoring_batch_size: int = 120
    # Fact-augmented key expansion (LongMemEval pattern)
    key_expansion_interval_hours: float = 0.5
    key_expansion_batch_size: int = 30
    # Knowledge crawl
    knowledge_crawl_enabled: bool = True
    knowledge_crawl_interval_hours: float = 3.0
    knowledge_crawl_batch_size: int = 5
    knowledge_crawl_delay: float = 10.0  # seconds between requests (be polite)

    # Link discovery (Phase 2) — queue interesting links found during crawling
    link_discovery_enabled: bool = True
    link_discovery_max_per_page: int = 5  # max links to queue per crawled page
    link_discovery_min_score: float = 0.4  # minimum score to queue a link

    # Entity web research (Phase 3) — targeted crawling for underdescribed entities
    entity_research_enabled: bool = True
    entity_research_interval_hours: float = 6.0  # 6h (was 12h)
    entity_research_batch_size: int = 5  # (was 3)
    entity_research_min_mentions: int = 5  # only research entities with 5+ mentions
    entity_research_cooldown_days: int = 14  # don't re-research within 14 days

    # Freshness re-crawl (Phase 4) — update stale crawled memories
    freshness_enabled: bool = True
    freshness_interval_hours: float = 12.0  # 12h (was 24h)
    freshness_batch_size: int = 5  # (was 3)
    freshness_max_age_days: int = 30  # re-crawl pages older than 30 days

    # Interest tracking (Phase 5) — prioritize crawling based on usage signals
    interest_tracking_enabled: bool = True
    interest_expansion_interval_hours: float = 12.0  # 12h (was 24h)
    interest_expansion_batch_size: int = 5  # (was 3)
    interest_signal_decay_days: int = 30  # signals older than 30 days lose weight

    # System pulse (autonomous health transmissions)
    system_pulse_interval_hours: float = 24.0
    # Community detection (GraphRAG). Full Leiden went 6h→weekly on
    # 2026-07-05: at 72k entities it blew the 90-min timeout on ~half its
    # runs (4 timeouts / 48h). The cheap community_assign job below keeps
    # new entities covered between full runs via single-step label
    # propagation (the Zep/Graphiti dynamic-extension pattern), so weekly
    # full refreshes only correct the slow drift.
    community_detection_interval_hours: float = 168.0
    # Incremental community assignment — pure SQL, assigns NULL-community
    # entities to the plurality community among their graph neighbors.
    community_assign_interval_hours: float = 6.0
    # Procedural memory distillation (2026-07-05, Memp/Foundry pattern) —
    # reviews recent lesson-like memories and distills repeatable
    # procedures (when-to-use + ordered steps) into procedural_memories.
    procedural_distill_interval_hours: float = 24.0
    procedural_distill_batch_size: int = 25   # source memories reviewed/run
    procedural_distill_max_new: int = 8       # new procedures cap/run
    # Memory observability (2026-07-05) — written-never-read stats, empty
    # search queries, search-trace retention.
    observability_interval_hours: float = 24.0
    search_trace_retention_days: int = 90
    # Live search enhancements (HyDE/expand/decompose) call the 27b on the
    # LIVE path. Their old 15-30s timeouts assumed an idle GPU; under
    # distill contention every auto-routed "how/why" query blocked the
    # full 30s and returned tier C anyway. If the model can't draft in
    # this many seconds, the enhancement isn't worth it — degrade to
    # plain hybrid. (2026-07-05, found the day auto_route went default-on.)
    # 6.0 → 2.5 (2026-07-22): under GPU contention an enhancement that
    # needs >2.5s costs more latency than its recall gain — degrade to
    # plain hybrid+rerank instead. Scheduler/eval paths set their own.
    live_enhancement_timeout_s: float = 2.5

    # L1 trust flywheel (2026-07-09). claim_kind_classifier feeds the
    # starved per-kind machinery (61% of active memories were NULL);
    # probe_generator unfreezes the hand-seeded 262-probe pool by
    # proposing probes for the checkable working set (http/file/
    # SELECT-sql auto-enabled, shell stored disabled for review);
    # stability_reinforce closes the verify-through-use loop (top-5
    # tier-A/B retrievals gain stability — previously decay-only).
    claim_kind_interval_hours: float = 6.0
    claim_kind_batch_size: int = 60
    probe_generator_interval_hours: float = 24.0
    probe_generator_batch_size: int = 15
    stability_reinforce_interval_hours: float = 12.0
    # Reconciliation sweeper (2026-07-09): old unverified stale-prone
    # memories vs newer same-entity memories → supersede/historicize.
    # The anti-recurrence for plan-vs-reality drift.
    reconciliation_interval_hours: float = 12.0
    reconciliation_batch_size: int = 20
    # Learned-context cards (C1, 2026-07-14): per-subject living briefs
    # served at session start. Only memories >= card_min_trust feed a
    # card; a subject needs card_min_sources memories to be worth one.
    card_builder_interval_hours: float = 8.0
    card_builder_batch_size: int = 25
    card_min_trust: float = 0.55
    card_min_sources: int = 4
    # card_factcheck (M1, 2026-07-14): cards get a published_accuracy
    # number — checkable claims verified mechanically (probe results)
    # or by LLM judge against the newest evidence. Below
    # card_min_accuracy the card is scheduled for rebuild with its
    # refuted claims injected as "do not restate".
    card_factcheck_interval_hours: float = 12.0
    card_factcheck_batch_size: int = 5
    card_factcheck_max_claims: int = 12
    card_factcheck_evidence_k: int = 6
    card_min_accuracy: float = 0.7

    # deep_recall (2026-07-06) — bounded multi-hop recall loop:
    # search → LLM reads the hits and emits ONE follow-up query naming
    # the bridging entity/aspect → search again → rerank the union
    # against the original query. Built after the include_related A/B
    # came back negative (entity-shared neighbors are too noisy a join
    # at relation F1 0.03); the loop reads *content* to find the bridge
    # instead of trusting graph edges. Deliberate tool, not the default
    # search path — expected latency 10-30s.
    deep_recall_max_hops: int = 2          # total search rounds (1 = plain search)
    deep_recall_per_hop_limit: int = 8     # results fetched per hop
    # Follow-up generation runs on the always-loaded CPU model
    # (qwen3-8b-cpu via llama-swap), NOT the contended 27b: during the
    # 2026-07-06 A/B every 27b follow-up call starved (76/76 ReadTimeouts
    # at 12s under scheduler load) and the loop silently degraded to
    # plain search. Query reformulation is an easy task; the CPU model
    # answers in 10-20s regardless of GPU state.
    deep_recall_followup_model: str = "qwen3-8b-cpu"
    deep_recall_followup_timeout_s: float = 25.0
    # Auto-optimize (search quality self-improvement)
    auto_optimize_interval_hours: float = 12.0
    # Co-occurrence relationship inference
    cooccurrence_interval_hours: float = 2.0  # 2h — still aggressive but leaves room for facts
    cooccurrence_batch_size: int = 40
    # GitHub incremental sync
    github_owner: str = ""  # GitHub username for sync (required for github_sync job)
    github_sync_interval_hours: float = 4.0  # 4h — catches active dev within a work session
    github_sync_quality_gate: bool = True  # LLM-score commits before storing, skip noise

    # Lesson classifier — tier-2 LLM pass that adds the `lesson` tag to
    # memories the tier-1 SQL backfill (keyword/category/commit-prefix)
    # missed. Runs conservatively: only tags when LLM is_lesson=True AND
    # confidence>=4. See scheduler_jobs.lesson_classifier() for details.
    lesson_classifier_interval_hours: float = 6.0
    lesson_classifier_batch_size: int = 10

    # Retrieval eval harness (2026-04-18) — golden-set Recall@10/MRR/nDCG
    # runs periodically so we spot regressions when swapping embedding
    # models, rerankers, or RRF/HNSW parameters. Weekly default.
    retrieval_eval_interval_hours: float = 168.0  # 7 days
    retrieval_eval_k: int = 10

    # Extraction eval harness (2026-04-20) — A/B current vs prior LLM on
    # a sample of memories, comparing entity/relationship overlap +
    # self-judge semantic equivalence. Weekly. Set
    # NOBRAINR_EXTRACTION_EVAL_INCUMBENT_MODEL to the pre-swap model so
    # we can catch quality regressions before committing to a new one.
    extraction_eval_interval_hours: float = 168.0  # 7 days
    extraction_eval_sample_size: int = 10
    extraction_eval_incumbent_model: str = "qwen3.5:35b"

    # Auto-negative outcome logging (2026-04-18) — feedback loop was dead
    # because nothing ever wrote was_useful=false, so the importance adjuster
    # had no variance to learn from. memory_search now auto-logs negatives
    # when retrieval is weak, fed by the same trace_id the UI uses so the
    # scheduler feedback job can down-rank misfiring memories.
    auto_negative_outcomes_enabled: bool = True
    # Log negative for every surfaced result when <N results come back. Low
    # recall is a signal that none of them were strongly relevant.
    auto_negative_low_recall_threshold: int = 3
    # Log negative for top-1 when reranker score is below this. Threshold
    # picked from the memory_search default threshold (0.3) — below that
    # the cross-encoder is telling us "nothing here really matches."
    auto_negative_low_rerank_threshold: float = 0.3
    # Context string written to memory_outcomes.context so the signal is
    # distinguishable from human feedback in the scheduler feedback loop.
    auto_negative_context_prefix: str = "auto:"

    # Monitoring & Alerts
    monitoring_enabled: bool = True
    monitoring_interval_hours: float = 1.0
    monitoring_email_enabled: bool = False
    monitoring_smtp_host: str = ""
    monitoring_smtp_port: int = 587
    monitoring_smtp_user: str = ""
    monitoring_smtp_password: str = ""
    monitoring_smtp_from: str = ""
    monitoring_smtp_to: str = ""  # comma-separated recipients
    monitoring_unhealthy_threshold: int = 3  # consecutive unhealthy checks before alerting


settings = Settings()
