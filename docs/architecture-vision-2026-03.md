# NOBRAINR ARCHITECTURAL VISION -- THE NORTH STAR

**Date:** 2026-03-08 | **State:** 3,441 memories, 8,047 entities, 9,176 relations, 18 scheduler jobs, 12 data sources

---

## 1. PERSONAL VAULT VISION

### Current Strengths
- 12 importers already operational (ChatGPT, Claude Code, Claude.ai, Affine, docx, Sticky Notes, Google Keep, websites, web crawls)
- Hybrid search (RRF fusion of vector + FTS) with quality-weighted relevance scoring
- Timeline view exists in dashboard
- RAG chat with source attribution

### Current Gaps
- **No temporal query understanding.** "What did I learn about Docker networking last month?" gets semantic results across all time, not time-scoped results. The `search_memories` function at `/root/nobrainr/src/nobrainr/db/queries.py:99` accepts no date parameters. The `memory_relevance` function in `/root/nobrainr/src/nobrainr/db/schema.py:241` uses a gentle 180-day half-life recency boost but cannot constrain to a date range.
- **No preference/personality layer.** All memories are treated identically in schema. A preference ("I prefer bun over npm") has the same schema as a technical fact ("PostgreSQL 18 uses pg_stat_progress_analyze"). No fast-path retrieval for preferences.
- **No continuous life-stream ingestion.** Importers are batch-oriented (file upload). No watch-and-import, no journaling integration, no health/habit data.
- **Timeline is a flat list.** TimelineView.vue renders memories in reverse chronological order with no narrative structure, no session grouping, no project arcs.
- **Agent briefings are query-dependent.** `session-start.sh` calls `/api/smart-recall` with 5 results. No persistent user profile, no preference injection, no adaptive context.

### Proposed Architecture

**A. Query Intent Parser (pre-search layer)**
Before embedding, parse the query to extract:
- Temporal expressions: "last month", "in January", "yesterday", "before we deployed" -> date_from/date_to
- Entity references: "Docker networking" -> entity lookup + memory-via-entity path
- Meta-questions: "how many memories about Python?" -> stats query, not search
- Preference queries: "what's my preferred package manager?" -> preference-type filter

Implementation: Add a `parse_query_intent()` function called before `embed_text()` in `memory_search`. Use regex patterns for temporal expressions (dateutil/dateparser library), entity name matching against the entities table, and keyword detection for meta-questions. No LLM needed for 90% of cases.

**B. Temporal-Aware Search**
- Add `date_from: datetime | None` and `date_to: datetime | None` parameters to `search_memories` in queries.py
- Modify the SQL WHERE clause to add `AND created_at >= $N AND created_at <= $M` when temporal bounds are present
- In the `memory_relevance` function, when a temporal window is specified, multiply `recency_boost` by 3x for memories inside the window and 0.1x outside
- New MCP tool: `memory_timeline(date_from, date_to, group_by="day|week|month")` -- groups memories by period, returns count + top-3 summaries per bucket

**C. Preference Memory Subsystem**
- New column: `memory_subtype TEXT` on the memories table (values: "preference", "fact", "decision", "pattern", "procedure", "narrative")
- Auto-classification: In `store_memory_with_extraction()`, scan content for preference patterns ("I prefer", "always use", "never do", "my default is") and set subtype accordingly
- Preferences get `importance=0.95` and are excluded from decay in `memory_decay`
- New API endpoint: `GET /api/preferences?source_machine=X` -- returns all active preference memories, sorted by recency. Injected into agent session context
- Preference conflict detection: When a new preference is stored, check for existing preferences with high similarity (>0.8) and contradicting content. Flag as contradiction, ask for resolution

**D. Life Logging Integration**
- **Daily note importer** (`importers/daily_notes.py`): Watches a configured directory for `.md` files with date-based filenames. Auto-imports new/changed files with `source_type="daily_note"`, `category="session-log"`. Supports Obsidian, Logseq, and plain markdown
- **Activity synthesis**: New scheduler job `daily_digest` (midnight, SQL+LLM): Collects all memories created that day, groups by category, generates a 3-sentence digest. Stored as `source_type="narrative"`. Enables "what was I working on March 5th?"
- **Weekly narrative**: Scheduler job producing a coherent weekly summary from daily digests. Project-aware: identifies dominant projects per week

**E. Personal Context for AI Assistants**
- **User profile entity**: A special entity `type="user_profile"` maintained by a scheduler job. Aggregates: expertise areas (from memory categories + entity types), active projects (entities with recent memories), communication preferences (from preference memories)
- **Structured briefing generator**: Instead of raw search results, produce a ~500-token briefing: "Expertise: [areas]. Active projects: [list]. Recent decisions: [list]. Preferences: [list]." Cached, regenerated hourly
- **Adaptive retrieval**: Track which memory categories agents actually access (via `last_accessed_at` patterns). Shift briefing mix toward high-utility categories over time. Current feedback data (778 positive, 0 negative) is not discriminating enough -- need to implement `was_useful: false` cases

---

## 2. AGENTIC ULTRA-TOOL VISION

### Current Strengths
- Mem0-style write path (ADD/UPDATE/SUPERSEDE/NOOP) prevents duplication on every store (`/root/nobrainr/src/nobrainr/extraction/dedup.py`)
- Quality scoring (specificity, actionability, self-containment) ranks agent-useful memories higher (`/root/nobrainr/src/nobrainr/scheduler_jobs.py:1068`)
- Cross-machine insights discover patterns spanning multiple agents (`scheduler_jobs.py:577`)
- Memory feedback loop (memory_outcomes table) adjusts future relevance
- Entity extraction creates a shared knowledge graph all agents can traverse

### Current Gaps
- **No multi-agent coordination.** Agents share a memory store but have no awareness of each other's active work. Two agents can work on the same problem simultaneously without knowing
- **No task memory.** No structured way to record "we tried approach X and it failed because Y" vs "approach Z worked"
- **No tool selection memory.** No way to record "for problem type P, tool T works best"
- **No code pattern library.** Code snippets are embedded in free-text memories. No structured extraction of reusable code patterns
- **No project context persistence.** Each agent session starts fresh, discovers project structure from scratch. CLAUDE.md files help but are manually maintained

### Proposed Architecture

**A. Multi-Agent Coordination**

*Agent registry:*
- New table: `agent_sessions (id, agent_id, session_id, machine, status, started_at, ended_at, active_task, metadata)`
- On session start, agents register via `log_event(event_type="session_start")`. On end, `log_event(event_type="session_end")`
- New MCP tool: `agent_status()` -- returns all active agent sessions with their current tasks. Agents check this before starting work to avoid duplication

*Conflict resolution:*
- When an agent stores a memory that SUPERSEDES another, and the superseded memory was created by a different agent in the last hour, flag as potential conflict
- New `agent_events` event_type: "conflict_detected" -- includes both memory IDs and the machines involved
- Agents can call `memory_search(tags=["needs-review"])` to find unresolved conflicts

*Shared task board:*
- Lightweight task tracking via memories with `memory_subtype="task"` and metadata fields: `{status: "open|in_progress|done|failed", assigned_to: agent_id, outcome: "..."}`
- MCP tool: `task_list(status="open")` -- returns active tasks. Agents claim tasks by updating status to in_progress with their agent_id

**B. Task Memory (What Worked / What Failed)**

*Approach tracking:*
- New memory_subtype: "approach_record"
- Structured metadata: `{problem_type: "...", approach: "...", outcome: "success|failure|partial", root_cause: "...", time_spent_estimate: "..."}`
- On task completion, agents call `memory_store` with approach_record subtype. The write path's dedup ensures we don't store duplicate approaches
- New MCP tool: `approach_search(problem_type)` -- semantic search filtered to approach_records, sorted by outcome (successes first) then recency

*Failure pattern detection:*
- Scheduler job: `failure_analysis` (6h interval). Finds approach_records with outcome="failure", clusters by problem_type, identifies repeated failure patterns
- Synthesizes anti-pattern memories: "When facing [problem type], avoid [approach] because [root cause]. Instead try [successful alternative]."

**C. Tool Selection Memory**

*Tool effectiveness tracking:*
- When agents use a tool and it works/fails, store with subtype="tool_record", metadata: `{tool_name, problem_type, effectiveness: 1-5, context}`
- New MCP tool: `best_tool(problem_type)` -- searches tool_records, returns tools ranked by effectiveness for that problem type
- Over time builds a tool recommendation engine: "For parsing HTML, use BeautifulSoup (4.8/5 from 12 uses) not regex (1.2/5 from 3 uses)"

**D. Code Pattern Library**

*Pattern extraction:*
- New entity_type: "code_pattern". Extracted from memories containing code blocks (detect via ``` markers)
- Pattern schema: language, pattern_name, problem_it_solves, code_template, anti_patterns
- Scheduler job: `pattern_extraction` -- scans memories with code blocks, extracts reusable patterns via LLM
- MCP tool: `code_pattern(language, problem)` -- returns matching patterns with examples

**E. Project Context Persistence**

*Automated CLAUDE.md generation:*
- For each project entity with 20+ memories, auto-generate a CLAUDE.md-style context document
- Scheduler job: `project_context_refresh` (12h) -- aggregates architecture decisions, file structure knowledge, known issues, deployment procedures from memories linked to each project entity
- Stored as source_type="project_context", tagged with the project name
- Agents call `memory_search(tags=[project_name], category="architecture")` at session start

*Codebase understanding persistence:*
- When agents discover architectural patterns (e.g., "this codebase uses repository pattern with asyncpg"), store as high-confidence architecture memory linked to the project entity
- Over time builds comprehensive project understanding that survives across agent sessions

---

## 3. WEB CRAWL & KNOWLEDGE BUILDING VISION

### Current Strengths
- 5-phase crawl system already operational: seed URLs, link discovery, entity research, freshness re-crawl, interest-based expansion
- Trusted domain whitelist prevents crawling the entire web (`/root/nobrainr/src/nobrainr/crawler/knowledge.py:61`)
- Link scoring system (`_score_link()`) evaluates discovered links by domain trust, path patterns, and depth
- Crawl4AI integration with JS rendering (Chromium) handles SPAs
- Interest signals table tracks what agents search for, driving proactive research

### Current Gaps
- **No knowledge gap detection.** The system crawls based on seed URLs and interest signals, but doesn't identify "we have 50 memories about Docker but zero about Docker Swarm" -- a clear knowledge gap
- **No domain expertise depth.** Crawling breadth-first across many domains rather than going deep on user's core areas
- **No trend detection.** Cannot identify "pgvector has released 3 versions since our last crawl" or "FastAPI 1.0 was announced"
- **No source credibility scoring.** All crawled content gets confidence=0.8 regardless of whether it's official docs, a blog post, or a forum answer
- **No content change tracking.** Freshness re-crawl detects changes but doesn't track what changed or why it matters

### Proposed Architecture

**A. Knowledge Gap Detection**

*Gap analysis scheduler job:*
- `knowledge_gaps` (24h interval): For each entity with 5+ mentions but few crawled memories, identify it as a knowledge gap
- Score gaps by: entity mention_count (demand), existing memory count (supply), entity type (technology/concept are more crawlable than person/file)
- Feed gaps into `entity_web_research` as high-priority candidates
- New API endpoint: `/api/knowledge-gaps` -- shows entities with high demand but low coverage. Dashboard widget

*Relationship gap detection:*
- If entity A relates to entity B, and we have deep knowledge of A but nothing about B, B is a gap
- Example: 30 memories about "pgvector" but 0 about "HNSW indexing parameters" -- the latter is clearly needed

**B. Domain Expertise Building**

*Expertise depth tracking:*
- New scheduler job: `expertise_assessment` (weekly). For each entity_type="technology" with 10+ memories, assess depth: surface (overview docs), intermediate (configuration, common issues), deep (internals, optimization, edge cases)
- Compare against a "depth template" per domain. For PostgreSQL: surface=overview, intermediate=indexing+EXPLAIN, deep=WAL+vacuuming+internal catalog
- Identify areas where we're stuck at surface level and auto-research the intermediate topics

*Curriculum-style crawling:*
- Instead of random link following, build a "learning path" for each technology: official docs -> getting started -> configuration -> troubleshooting -> internals
- The knowledge_crawl job already has seed URLs. Extend with depth-aware URL selection: if we have overview docs, next crawl targets should be intermediate topics

**C. Trend Detection**

*Changelog monitoring:*
- Maintain a list of changelog URLs (already partially in SEED_URLS: Python whatsnew, PostgreSQL release notes, Docker release notes)
- Dedicated scheduler job: `changelog_monitor` (24h). Re-crawls changelog pages, diff against stored content, extract new items
- When a new version is detected, store a "version_update" memory and trigger research on new features

*RSS/Atom feed support:*
- Add RSS feed parsing to the crawler. Monitor feeds for key technologies
- New table: `feed_subscriptions (url, last_checked, etag, last_modified)`
- Lower priority than changelog monitoring but broader coverage

**D. Source Credibility Scoring**

*Domain-level credibility:*
- Extend the TRUSTED_DOMAINS concept into a credibility score:
  - 1.0: Official documentation (docs.python.org, www.postgresql.org)
  - 0.8: Official GitHub repos (github.com/organization/repo)
  - 0.7: Well-known community resources (MDN, Stack Overflow accepted answers)
  - 0.5: Blog posts from recognized authors
  - 0.3: Generic blog posts, forums
- Store as `confidence` on the memory. The quality_scoring job already considers confidence in relevance ranking

*Content freshness indicator:*
- Extract publication dates from crawled pages (meta tags, visible dates)
- Store in metadata: `{published_date, crawled_date, estimated_freshness}`
- Memories from 2024 docs about a 2026 technology get lower confidence

**E. Knowledge Freshness Management**

*Change tracking:*
- When freshness_recrawl detects content changes, store a diff summary: "PostgreSQL docs updated: new section on pg_stat_io, removed deprecated pg_stat_progress_vacuum fields"
- Link the diff to the original memory via metadata
- New memory subtype: "content_update" -- tracks what changed and when

*Staleness detection:*
- For each technology entity, track the "knowledge age" = average age of memories about it
- If knowledge_age > 90 days and the technology is actively developed, flag for re-research
- Dashboard widget: "Stale knowledge" showing technologies needing updates

---

## 4. CHAT INTERFACE VISION

### Current Strengths
- RAG chat pipeline operational (`/root/nobrainr/src/nobrainr/chat/rag.py`): sanitize -> embed -> hybrid search -> context build -> Ollama stream -> source attribution
- Source attribution: Shows entities and memories used to generate each response
- Injection protection via sanitize.py
- Chat panel integrated into dashboard with entity focus-on-click

### Current Gaps
- **No search explanation.** The system returns results but doesn't explain why they're relevant. The user sees similarity scores but no human-readable explanation
- **No knowledge graph navigation in chat.** Cannot say "show me everything connected to Docker" and get a graph traversal
- **No cross-reference discovery.** "This relates to what you learned about X" -- the system doesn't volunteer connections
- **No confidence calibration.** Every response has the same authority regardless of whether it's backed by 1 memory or 20
- **No proactive suggestions.** The system only responds to queries, never volunteers information
- **Plain text output only.** No markdown rendering, no code highlighting, no structured responses

### Proposed Architecture

**A. Search with Explanation**

*Relevance explanation:*
- When returning search results (both in MCP and chat), add a `relevance_reason` field
- Components: "High semantic similarity (0.87)" + "Contains your search terms" + "Frequently accessed" + "High quality score (0.8)" + "Recent (2 days ago)"
- Implementation: The `_row_to_dict` function in queries.py already returns similarity and relevance. Add a `_explain_relevance()` function that generates a human-readable string from the component scores

*In chat responses:*
- After generating a response, append a "Sources & confidence" section: "Based on 8 memories (3 high-confidence, 5 moderate). Key sources: [list with dates and categories]"
- When the context is weak (few memories, low similarity), say so: "I found limited information about this topic. Here's what I have, but it may be incomplete."

**B. Knowledge Graph Navigation in Chat**

*Graph query commands:*
- Detect graph navigation intents in chat: "what connects to Docker?", "show me the Docker ecosystem", "how is PostgreSQL related to pgvector?"
- Route to `entity_graph` traversal instead of standard memory search
- Format graph results as narrative: "Docker connects to 15 entities. Key relationships: deploys nobrainr, runs pgvector, configured with docker-compose. The Docker cluster also includes Traefik, Coolify, and Ollama."

*Entity-centric responses:*
- When a query clearly targets an entity, first look up the entity description and connections, then augment with memory search
- "Tell me about nobrainr" -> entity description + connected entities + top 5 memories, not just memory search

**C. Cross-Reference Discovery**

*Proactive connection surfacing:*
- After generating a response, check if any entities in the response also appear in memories the user hasn't seen
- "This relates to what you learned about [entity] in [memory summary]." -- append as a "Related" section
- Implementation: After RAG response, collect entity IDs from source memories. For each entity, fetch other memories not already in context. If any have high quality_score, suggest them

*"You might also want to know" feature:*
- When a search returns results, also run a secondary search on the top entities mentioned in results
- Present as "Related topics" -- clickable in the dashboard, expandable in MCP

**D. Confidence-Calibrated Responses**

*Confidence scoring for responses:*
- Before responding, calculate response confidence:
  - High (0.8+): 5+ memories with similarity > 0.7, from multiple sources, consistent content
  - Medium (0.5-0.8): 2-4 memories, moderate similarity, or single source
  - Low (<0.5): 0-1 memories, low similarity, or contradicting sources
- Prepend confidence indicator to chat responses: "[High confidence - 8 sources]" or "[Low confidence - limited information]"
- In MCP search results, add `response_confidence` to the return dict

**E. Proactive Suggestions**

*Interest-based suggestions:*
- The interest_signals table already tracks what agents search for. Surface trending topics in the dashboard
- New scheduler job: `proactive_insights` (12h). Analyzes recent interest signals, finds memories that match trending topics but haven't been accessed recently. Stores as source_type="suggestion"
- Dashboard widget: "You might want to review" -- shows high-quality memories related to recent interests that haven't been accessed

*Contradiction alerts:*
- When contradiction_detection finds genuine conflicts, surface them proactively in the dashboard
- New API: `/api/contradictions` -- returns unresolved contradictions for human review

---

## 5. EVALUATION CRITERIA & DESIGN PRINCIPLES

### Memory Quality Metrics

**A. Precision & Recall for Retrieval**
- **MRR (Mean Reciprocal Rank):** For known-answer queries, where does the correct memory appear in results? Track via the feedback system -- when agents report `was_useful: true`, record the rank position
- **NDCG (Normalized Discounted Cumulative Gain):** Weight useful results higher when they appear earlier in the list. Current feedback is binary (useful/not) -- extend to include the rank at which the result was found
- **Implementation:** Add `result_rank: int` to `memory_outcomes` table. Agents report both `was_useful` and `which result position was useful`. Compute MRR/NDCG weekly as a scheduler job

**B. Memory Quality Distribution**
- Target distribution: 70% quality_score >= 0.6, 20% between 0.4-0.6, 10% below 0.4
- Currently: 1,836 scored out of 3,441 total. The `quality_scoring` job runs every 30min with batch=30 -- will take ~16 more hours to score everything
- Track quality distribution over time. If low-quality memories accumulate, tighten the write-path LLM to reject more

**C. Staleness Metrics**
- **Knowledge freshness:** Average age of memories per entity/category, weighted by access frequency
- **Dead memory ratio:** Memories never accessed after 30 days / total memories. Currently tracked by `memory_decay` job
- Target: <15% dead memory ratio. Currently 24 archived out of 3,441 (0.7%) -- but this is because decay is conservative. The quality_score system should eventually drive this higher

### Knowledge Graph Health Metrics

**A. Connectivity**
- **Average degree:** edges per entity. Currently ~2.3 (9,176 relations / 8,047 entities * 2). Target: 3-5 for a useful graph
- **Giant component ratio:** What fraction of entities are in the largest connected component? Higher = better connected. Compute via NetworkX in the layout job
- **Orphan entity ratio:** Entities with 0 connections / total entities. Should decrease over time as extraction quality improves

**B. Coverage**
- **Entity coverage:** What fraction of memories have at least one extracted entity? Currently 1,836 extracted / 3,441 total = 53%. Target: >90%
- **Relation coverage:** Average relations per extracted memory. Track via entity_memories join

**C. Accuracy**
- **Extraction quality validation:** The `extraction_quality` scheduler job already samples and validates. Track validation pass rate over time. Target: >85%
- **Entity merge rate:** How many duplicate entities does `entity_merging` find per cycle? Decreasing rate = improving extraction quality

### User Satisfaction Signals

**A. Explicit Feedback**
- Current: 778 feedback records, 100% positive -- this is not discriminating. Need to make negative feedback easier
- Add "not helpful" button on search results in dashboard
- MCP instructions should encourage agents to report both useful AND not-useful results

**B. Implicit Signals**
- **Access patterns:** Frequently accessed memories are valuable. Track `access_count` distribution
- **Search refinement:** If an agent searches, gets results, then immediately searches again with different terms, the first search was unsatisfying
- **Time-to-use:** How long after search does the agent report feedback? Quick feedback = clear result. Delayed = uncertain

### System Efficiency Metrics

**A. LLM Cost per Insight**
- Track LLM calls per scheduler job per cycle. Currently: each job makes N calls (batch_size), each call ~7s on gemma3:12b
- Calculate: total LLM time / valuable outputs. For synthesis: if 5 candidates produce 2 insights, cost = 5*7s / 2 = 17.5s per insight
- Target: <30s wall-clock per valuable output for structured labeling jobs, <2min for reasoning jobs

**B. Storage Efficiency**
- **Bytes per valuable memory:** Total DB size / (memories with quality_score >= 0.6). Track this as the DB grows
- **Embedding storage:** 768 dimensions * 4 bytes * 3,441 memories = ~10MB for memory embeddings, ~25MB for entity embeddings. Well within budget
- **Index size:** HNSW indexes grow with data. Monitor via pg_relation_size. At 50K memories, HNSW index will be ~150MB -- still manageable

### Design Principles

1. **Never delete, always decay.** Low-quality memories fade from search results via quality_score weighting, but the data remains for potential future value. Enforced by the MEMORY.md rule and settings.json deny list.

2. **Quality gates, not quantity limits.** Don't cap memory count. Instead, ensure the write-path (ADD/UPDATE/SUPERSEDE/NOOP) prevents duplication, quality_scoring identifies garbage, and memory_relevance ranks the good stuff to the top.

3. **Local-first AI.** All LLM operations run on the local RTX 4000 SFF Ada via Ollama. No external API calls for core functionality (embedding, extraction, scheduling). External APIs (Anthropic, OpenAI) are used only by the agents themselves, not by nobrainr.

4. **Polite automation.** Scheduler jobs use semaphores (3 concurrent LLM slots out of 6), inter-request delays (1s), and staggered starts to coexist with live requests. Crawling respects rate limits (10s between requests) and only touches trusted domains.

5. **Schema-first evolution.** New features add columns/tables, never modify existing ones. Migrations are idempotent (IF NOT EXISTS, ALTER TABLE ADD COLUMN IF NOT EXISTS). The schema in `/root/nobrainr/src/nobrainr/db/schema.py` is designed for forward compatibility.

6. **Feedback closes the loop.** Every retrieval operation should eventually produce a feedback signal. This is the only way to know if the system is useful. Current 778/778 positive rate means we're not collecting negative feedback -- fix this first.

7. **Graph is the differentiator.** Mem0, Supermemory, and other competitors have vector search. nobrainr's entity extraction + knowledge graph + Louvain community detection + graph visualization is unique in the MCP memory server space. Invest in graph quality (extraction accuracy, entity merging, relationship types) over raw memory count.

---

## 6. RISK ASSESSMENT

### A. Knowledge Graph Pollution

**Risk:** Low-quality entities and wrong relationships degrade graph utility. Currently 8,047 entities -- at 50K+, noise will dominate signal.

**Current mitigations:**
- `extraction_quality` job validates entity-memory links (20/batch, 4h interval)
- `entity_pruning` removes entities with 1 memory and 0 relations after 24h
- `entity_merging` deduplicates entities with high embedding similarity
- Unique constraint on `(canonical_name, entity_type)` prevents exact duplicates

**Additional mitigations needed:**
- Entity type accuracy: The extraction prompt (`/root/nobrainr/src/nobrainr/extraction/extractor.py:10`) allows 14 entity types. Some are too similar (service vs container vs project). Reduce to 8-10 well-differentiated types
- Relationship type normalization: 12 relationship types but LLM sometimes invents new ones not in the schema. The ExtractionResult Pydantic model enforces the Literal type, but malformed JSON from Ollama could bypass this
- Entity importance scoring: Add an `importance` column to entities (similar to memories). Compute from: mention_count, memory quality of linked memories, relationship count. Low-importance entities get lower visibility
- Rate of graph growth: Track entities_created_per_day. If it spikes (e.g., during a large import), extraction quality likely drops. Throttle extraction during bulk imports

### B. Embedding Drift

**Risk:** If the embedding model changes (e.g., upgrading from nomic-embed-text to a newer model), old embeddings become incompatible with new ones. Search quality degrades immediately.

**Current state:** nomic-embed-text (768 dimensions) is the only model ever used. No migration infrastructure exists.

**Mitigations needed:**
- **Embedding versioning:** Add `embedding_model TEXT DEFAULT 'nomic-embed-text'` column to both memories and entities tables. On model change, know which embeddings are stale
- **Batch re-embedding tool:** CLI command `nobrainr re-embed --model new-model --batch-size 50` that re-embeds all memories with the new model. Must handle the 3,441 memories (~23ms each = ~80 seconds total for embeddings alone, but including entity re-embedding it's ~8,047 * 23ms = ~3 minutes)
- **Dual-index period:** During migration, search against both old and new embeddings, merge results. Remove old embeddings after full migration
- **Lock on model change:** Prevent writes during re-embedding to avoid mixed embeddings

### C. LLM Hallucination in Entity Extraction

**Risk:** gemma3:12b (the extraction model) may hallucinate entities not present in the source text, invent relationships, or misclassify entity types.

**Current mitigations:**
- `think=False` for extraction (structured labeling, not reasoning) -- reduces creative hallucination
- `extraction_quality` job validates samples
- Pydantic model validation (ExtractionResult) enforces schema

**Measured impact:** The extraction_quality job reports invalid extractions, but we don't currently aggregate this into a quality rate. Based on the entity_pruning job removing 1-memory entities, there's meaningful noise.

**Additional mitigations needed:**
- **Confidence-gated extraction:** Only create entities with extraction confidence >= 0.5 (currently all extractions are treated as confidence=1.0)
- **Entity validation pass:** After extraction, verify each entity name appears in the source text (case-insensitive substring match). Reject entities that don't appear
- **Relationship grounding:** Each relationship's source and target must match extracted entity names exactly (this is already enforced in the pipeline, good)
- **Human review queue:** High-mention-count entities that fail quality validation should be flagged for human review rather than auto-pruned

### D. Privacy Concerns with Personal Data

**Risk:** nobrainr stores personal data from ChatGPT conversations, sticky notes, documents, and daily activities. If the server is compromised, all personal knowledge is exposed.

**Current mitigations:**
- VPN-only access (wg0 interface, UFW firewall, Traefik vpn-only middleware)
- No authentication on the API (relies entirely on network-level access control)
- Iptables DOCKER-USER rules block external access to port 8000/8100

**Gaps:**
- **No API authentication.** Any device on the VPN can read/write all memories. If a VPN client is compromised, full database access
- **No data classification.** Personal notes, health data, and technical documentation all have the same access level
- **No encryption at rest.** PostgreSQL data is stored unencrypted on disk. Full disk encryption (LUKS) would mitigate physical access, but isn't configured
- **No audit log.** No record of who accessed what memory. The `last_accessed_at` field tracks access time but not the accessor

**Recommended mitigations:**
- **Bearer token authentication:** Add a simple API key check on all API and MCP endpoints. Store hashed tokens in DB
- **Sensitive memory flag:** Add `is_sensitive BOOLEAN DEFAULT FALSE` to memories. Sensitive memories require explicit access, excluded from default search results
- **Access audit log:** Log all memory reads (IP, timestamp, memory_id) to a separate table. Low overhead, high forensic value
- **Backup encryption:** The Coolify backup system should encrypt backups before writing to storage

### E. Scale Challenges at 50K+ Memories, 100K+ Entities

**Risk:** Current architecture was designed for thousands, not hundreds of thousands. Performance degradation is likely in search, graph layout, and scheduler jobs.

**Current performance at 3.4K memories / 8K entities:**
- Semantic search: <100ms (HNSW index, ef_search=40)
- Hybrid RRF search: ~200ms (two queries + merge)
- Graph layout: 2-3 seconds for 7K nodes (NetworkX, computed on each request)
- Extraction: ~7s per memory (Ollama structured output)
- Scheduler cycle: 15 LLM jobs * batch_size * 7s each -- but limited by semaphore(3)

**Projected issues at 50K memories / 100K entities:**

1. **HNSW index rebuild time.** HNSW index with m=24, ef_construction=128 on 50K 768-dim vectors takes ~30 seconds to rebuild. During REINDEX, searches are slower. Not a problem unless we add/rebuild indexes frequently. Current HNSW parameters are fine for 100K+

2. **Graph layout computation.** NetworkX on 100K nodes is expensive. `compute_graph_layout()` uses Louvain + spring_layout. At 100K nodes, this could take 30+ seconds. **Solution:** Pre-compute layout as a scheduler job, cache results. Invalidate cache only when entity count changes by >5%

3. **Scheduler batch scaling.** At 50K memories, `get_unsummarized_memories(20)` is fine (indexed query). But `get_similar_memory_pairs(threshold=0.88, limit=10)` does a cross-product similarity search which is O(n^2). **Solution:** Use the HNSW index for candidate generation instead of brute-force

4. **Full-text search.** GIN index on 50K memories handles tsquery efficiently. Not a concern

5. **Memory overhead.** 50K memories * 768 dims * 4 bytes = ~150MB for embeddings. 100K entities * 768 * 4 = ~300MB. Plus HNSW indexes (~2x data size). Total ~1GB. Well within the 62GB RAM budget

6. **Write amplification.** Every store_memory_with_extraction calls decide_write_action (one LLM call) + embed_text (one Ollama call) + process_memory (one LLM call + N embed calls). At high ingestion rates (e.g., 100 memories/hour during bulk import), this creates LLM contention. **Solution:** Queue-based extraction with configurable parallelism (already have _extraction_semaphore=1, could increase)

**Recommended scale preparations:**
- Add a materialized view for graph data, refreshed by scheduler
- Implement cursor-based pagination everywhere (currently using OFFSET which is O(n))
- Add connection pooling metrics (currently using asyncpg pool but not monitoring pool saturation)
- Consider HNSW parameter tuning: at 100K+ vectors, increase ef_construction to 200 and m to 32 for better recall

---

## IMPLEMENTATION PRIORITY (Recommended Order)

### Phase 1: Foundations (Next 2 Weeks)
1. `memory_subtype` column on memories table (preferences, facts, decisions, approaches)
2. `date_from`/`date_to` parameters on search_memories
3. Bearer token authentication on API/MCP endpoints
4. Fix feedback -- add negative feedback flow, track result rank
5. Entity importance scoring column + computation

### Phase 2: Personal Vault (Weeks 3-4)
6. Preference extraction and /api/preferences endpoint
7. Query intent parser (temporal expressions, entity references)
8. Daily digest scheduler job
9. Agent briefing generator (structured ~500-token context)

### Phase 3: Agentic (Weeks 5-6)
10. Agent session registry + agent_status() MCP tool
11. Approach tracking (what worked/failed) memory subtype
12. Tool effectiveness tracking
13. Confidence-calibrated chat responses

### Phase 4: Knowledge Building (Weeks 7-8)
14. Knowledge gap detection scheduler job
15. Source credibility scoring (domain-level confidence)
16. Changelog monitoring for key technologies
17. Change tracking for freshness re-crawl

### Phase 5: Scale Prep (Weeks 9-10)
18. Materialized view for graph data
19. Cursor-based pagination
20. Embedding versioning infrastructure
21. Graph layout caching

---

This document represents the north star for nobrainr development. Each section can be implemented incrementally -- the current architecture is well-structured for extension, with clean separation between MCP tools (`/root/nobrainr/src/nobrainr/mcp/server.py`), the shared memory service (`/root/nobrainr/src/nobrainr/services/memory.py`), database queries (`/root/nobrainr/src/nobrainr/db/queries.py`), scheduler jobs (`/root/nobrainr/src/nobrainr/scheduler_jobs.py`), and the dashboard API (`/root/nobrainr/src/nobrainr/dashboard/api.py`).

The single most impactful change is **fixing the feedback loop** (items 4 and 5 in Phase 1). Without discriminating feedback, all the quality scoring, relevance ranking, and adaptive retrieval operates blind. Everything else builds on knowing what's actually useful to users and agents.
