# nobrainr Roadmap — March 2026

> Generated from competitive analysis of Mem0 v1.0, Microsoft GraphRAG, and real-world usage patterns.

---

## Phase 1: Search Quality (Quick Wins)

### 1.1 Dynamic Recall Thresholding — COMPLETED (60a257f)
**Priority:** HIGH | **Effort:** ~10 lines | **Impact:** Major search quality improvement

Drop search results below 50% of the top result's similarity score. Currently we use a fixed threshold (0.3) which lets irrelevant results through.

**Files:** `db/queries.py` (search_memories, search_facts)
**Inspiration:** Mem0 PR #4302 — raised auto-recall threshold to 0.6 and added dynamic scoring

### 1.2 Pre-Extraction Content Filtering — COMPLETED (60a257f)
**Priority:** HIGH | **Effort:** ~30 lines | **Impact:** Save GPU time, prevent noise at source

Filter noise from ChatGPT conversations BEFORE sending to gemma3 for extraction. Currently we filter entities AFTER extraction — wasting GPU on noise content.

Add to the distill pipeline:
- Strip system messages, timestamps, routing logs
- Drop single-word acknowledgments ("ok", "thanks", "sure")
- Skip extraction when no meaningful user content remains

**Files:** `services/distill.py`, `extraction/pipeline.py`
**Inspiration:** Mem0 PR #4302 — `isNoiseMessage` → `isGenericAssistantMessage` → `stripNoiseFromContent`

### 1.3 Temporal Anchoring in Extraction — COMPLETED (60a257f)
**Priority:** MEDIUM | **Effort:** ~15 lines | **Impact:** Better temporal awareness in search

Add timestamps to extraction prompts so extracted entities and relations carry temporal context. "The user used QGIS in March 2026" vs just "the user uses QGIS".

**Files:** `extraction/extractor.py` (system prompt)
**Inspiration:** Mem0 — "temporal anchoring, outcome-over-intent extraction"

---

## Phase 2: Graph Quality

### 2.1 Leiden Clustering (replace Louvain)
**Priority:** MEDIUM | **Effort:** ~20 lines + `pip install leidenalg` | **Impact:** Better community structure

Leiden algorithm produces tighter, more stable communities than Louvain. Same API surface — drop-in replacement.

**Files:** `services/communities.py`
**Dependency:** `leidenalg` package (requires igraph)
**Inspiration:** Microsoft GraphRAG uses Leiden exclusively

### 2.2 Hierarchical Community Detection
**Priority:** LOW | **Effort:** ~100 lines | **Impact:** Multi-level topic browsing

GraphRAG uses nested community levels — communities within communities. Enables progressive disclosure in the graph UI (zoom from topic → subtopic → entities).

**Files:** `services/communities.py`, `dashboard/api.py`, `GraphView.vue`
**Inspiration:** GraphRAG hierarchical Leiden clustering

### 2.3 DRIFT Search (Community-Aware Local Search)
**Priority:** LOW | **Effort:** ~150 lines | **Impact:** Better answers for complex queries

Microsoft's DRIFT search combines community summaries with entity-level search. Starts broad (community reports), generates follow-up questions, then drills into specific entities. More comprehensive than our current `global_search` (pure map-reduce).

**Files:** New `services/drift_search.py`, `mcp/server.py`
**Inspiration:** GraphRAG DRIFT Search paper

---

## Phase 3: Quality Scoring & Cleanup

### 3.1 Accelerate Quality Scoring — COMPLETED (60a257f)
**Priority:** HIGH | **Effort:** Config change | **Impact:** Get 78% unscored → 0%

Increase `quality_scoring_batch_size` from 30 → 100 and interval from 0.5h → 0.25h once ChatGPT distill backlog clears. The 78% unscored gap means we can't effectively filter by quality.

**Files:** `config.py`

### 3.2 Outcome-Over-Intent Extraction — COMPLETED (60a257f)
**Priority:** MEDIUM | **Effort:** ~10 lines prompt change | **Impact:** Higher quality extracted facts

Modify extraction prompt to prefer "what happened" over "what was planned". Currently the LLM extracts intentions and plans as facts, which age poorly.

**Files:** `extraction/extractor.py`, `scheduler_jobs.py` (fact extraction prompt)
**Inspiration:** Mem0 PR #4302 — explicit instruction to extract outcomes not intents

### 3.3 Category Refinement
**Priority:** LOW | **Effort:** ~50 lines SQL + migration | **Impact:** Better dashboard filtering

"Tooling" has 9,147 memories (43%). Consider splitting into subcategories or adding a secondary classification layer. Options:
- Auto-tag based on content patterns (QGIS, Docker, Python, Git)
- Two-level categories: "tooling/python", "tooling/qgis"
- Or just improve the tag system to compensate

**Files:** `utils/categories.py`, dashboard filters

---

## Phase 4: Performance & Scalability

### 4.1 Semaphore Fairness — COMPLETED (60a257f)
**Priority:** MEDIUM | **Effort:** ~30 lines | **Impact:** Even GPU distribution across jobs

Add a cooldown or round-robin mechanism so extraction doesn't monopolize the GPU semaphore for hours. Currently extraction runs continuously and starves other LLM jobs (facts, community summaries, quality scoring).

Options:
- After each extraction batch, `asyncio.sleep(5)` before re-acquiring
- Per-job cooldown tracking — don't run the same job twice in a row
- Priority queue with weights per job type

**Files:** `scheduler.py`

### 4.2 Fact Extraction Batch Size
**Priority:** LOW | **Effort:** Config change | **Impact:** Faster fact coverage

Increase `fact_extraction_batch_size` from 20 → 40 once extraction backlog clears. Current rate: ~1,500 facts/day. At 40/batch: ~3,000/day. Full coverage in ~2 weeks vs ~4 weeks.

**Files:** `config.py`

---

## Phase 5: Dashboard & Visualization

### 5.1 Community Summary Regeneration on Deploy
**Priority:** HIGH | **Effort:** Already done (app.py lifespan) | **Status:** COMPLETED

Graph cache pre-warms on startup. Community summaries regenerate on scheduled `community_detection` runs.

### 5.2 Fact Display in Dashboard
**Priority:** HIGH | **Effort:** Already done | **Status:** COMPLETED

Facts shown in MemoryDetail component with amber lightning bolt icons. `fact_search` MCP tool registered.

### 5.3 Community Topic Map View
**Priority:** MEDIUM | **Effort:** Already done | **Status:** COMPLETED

Topics/Entities toggle in graph toolbar. Click community → drill into entities.

### 5.4 Graph Label Improvements
**Priority:** LOW | **Effort:** ~20 lines CSS/Vue | **Impact:** Better readability

- Show community title labels at low zoom (overview level)
- Fade entity labels based on zoom level
- Cluster label positioning at community centroid

**Files:** `GraphView.vue`

---

## Completed This Session (March 16-20, 2026)

| Change | Commit |
|--------|--------|
| Wire fact_search MCP tool + dashboard fact display | `6e302da` |
| Fix 24h keep_alive for all Ollama callers | `0b85863` |
| Louvain resolution tuning (tested 2.5, reverted to 1.0) | `7353d40` |
| Remove hardcoded GitHub username | `4e954d6` |
| Expand entity noise filter (+100 terms, min length 2→3) | `f433d0e` |
| Pre-warm graph cache on startup | `3c9172e` |
| Community topic map view with drill-down | `341b15c` |
| Re-enable audit triggers | SQL direct |
| Cold-tier 284 low-quality memories | SQL direct |
| Prune 364 noise entities | SQL direct |

## Current Stats (March 20, 2026)

| Metric | Value |
|--------|-------|
| Memories | 31,101 |
| Entities | 14,666 |
| Relations | 51,527 |
| Facts | 7,175 |
| Communities (named) | 26 |
| Quality scored | 22% |
| ChatGPT distill remaining | ~10K conversations |
| ETA to distill completion | ~March 24-25 |
