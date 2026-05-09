<template>
  <div class="florilegium-page">
    <div class="florilegium-shell">

      <!-- LEFT — INDEX -->
      <aside class="florilegium-index">
        <div class="index-head">
          <div class="masthead-row">
            <span class="folio-label">Florilegium</span>
            <span class="index-count">
              <em>{{ memories.length }}</em> {{ memories.length === 1 ? 'entry' : 'entries' }}
            </span>
          </div>
          <h2 class="index-title">The Gathering</h2>

          <div class="search-row">
            <span class="search-glyph">⚹</span>
            <input
              v-model="searchQuery"
              class="folio-input"
              type="text"
              placeholder="search the gathering…"
            />
          </div>

          <div class="filter-row">
            <select v-model="categoryFilter" class="folio-select">
              <option :value="null">All categories</option>
              <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
            </select>
            <select v-model="machineFilter" class="folio-select">
              <option :value="null">All machines</option>
              <option v-for="m in machines" :key="m" :value="m">{{ m }}</option>
            </select>
          </div>

          <div class="quality-row">
            <span class="quality-label">Min quality</span>
            <input
              type="range" min="0" max="1" step="0.05"
              v-model.number="qualityFilter"
              class="folio-slider"
            />
            <span class="quality-value">
              {{ qualityFilter > 0 ? Math.round(qualityFilter * 100) + '%' : '—' }}
            </span>
          </div>
        </div>

        <div class="index-rule" />

        <div class="index-list">
          <template v-if="loading">
            <div v-for="n in 6" :key="n" class="card-skeleton" />
          </template>
          <template v-else-if="memories.length">
            <div class="memories-stagger cp-stagger" style="display:contents">
              <MemoryCard
                v-for="(m, i) in memories"
                :key="m.id"
                :memory="m"
                :selected="selectedMemory?.id === m.id"
                :highlight="searchQuery"
                :style="staggerStyle(i)"
                @click="selectMemory(m.id)"
              />
            </div>
            <div v-if="hasMore" class="load-more-row">
              <button class="folio-button" :disabled="loadingMore" @click="loadMore">
                <Dotty v-if="loadingMore" />
                <span v-else>turn the page</span>
              </button>
            </div>
            <div v-else-if="memories.length >= 50" class="end-of-list">
              <span class="ornament-sm">⸻</span>
              <p>— end of the gathering —</p>
            </div>
          </template>
          <div v-else class="index-empty">
            <span class="empty-mark">❦</span>
            <p>— this query finds no entry —</p>
            <p class="empty-hint">Try fewer constraints, or clear a filter to broaden the gathering.</p>
          </div>
        </div>
      </aside>

      <!-- RIGHT — PAGE -->
      <main class="florilegium-page-right">
        <template v-if="detailLoading">
          <div class="page-loading">
            <Dotty />
            <p class="loading-text">opening the page</p>
          </div>
        </template>
        <template v-else-if="selectedMemory">
          <MemoryDetail
            :memory="selectedMemory"
            :entities="selectedEntities"
            :facts="selectedFacts"
            @update="handleUpdate"
            @delete="handleDelete"
          />
        </template>
        <div v-else class="page-empty">
          <span class="ornament">❦</span>
          <h3 class="empty-title">An open page</h3>
          <p class="empty-tagline">Choose an entry from the gathering to read it in full.</p>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { watch, onMounted } from 'vue'
import { useMemories } from '@/composables/useMemories'
import { useStatsStore } from '@/stores/stats'
import { useSSE } from '@/composables/useSSE'
import MemoryCard from '@/components/MemoryCard.vue'
import MemoryDetail from '@/components/MemoryDetail.vue'
import Dotty from '@/components/Dotty.vue'
import { staggerStyle } from '@/composables/useStaggerIndex'

const statsStore = useStatsStore()
const {
  memories,
  selectedMemory,
  selectedEntities,
  selectedFacts,
  loading,
  loadingMore,
  hasMore,
  detailLoading,
  searchQuery,
  categoryFilter,
  machineFilter,
  qualityFilter,
  categories,
  machines,
  fetchMemories,
  loadMore,
  fetchMemoryDetail,
  updateMemory,
  deleteMemory,
  fetchCategories,
  fetchMachines,
} = useMemories()

function buildParams() {
  const params: Record<string, string | number> = {}
  if (searchQuery.value) params.q = searchQuery.value
  if (categoryFilter.value) params.category = categoryFilter.value
  if (machineFilter.value) params.source_machine = machineFilter.value
  if (qualityFilter.value > 0) params.min_quality = qualityFilter.value
  return params
}

async function selectMemory(id: string) {
  await fetchMemoryDetail(id)
}

async function handleUpdate(body: Record<string, unknown>) {
  if (!selectedMemory.value) return
  await updateMemory(selectedMemory.value.id, body)
  await fetchMemories(buildParams())
}

async function handleDelete() {
  if (!selectedMemory.value) return
  await deleteMemory(selectedMemory.value.id)
  await fetchMemories(buildParams())
}

let searchTimeout: ReturnType<typeof setTimeout>
watch(searchQuery, () => {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => fetchMemories(buildParams()), 300)
})

watch([categoryFilter, machineFilter, qualityFilter], () => {
  fetchMemories(buildParams())
})

useSSE((evt) => {
  if (['memory_created', 'memory_updated', 'memory_deleted'].includes(evt.type)) {
    fetchMemories(buildParams())
    fetchCategories()
    fetchMachines()
  }
})

onMounted(async () => {
  await statsStore.fetchStats()
  fetchCategories()
  fetchMachines()
  fetchMemories()
})
</script>

<style scoped>
.florilegium-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  min-height: calc(100vh - 64px);
}

.florilegium-shell {
  display: grid;
  grid-template-columns: 420px 1fr;
  /* Min-height fills the viewport, but the shell can grow when the
     index list is long. Body handles scroll → bottom items are
     always reachable, no stranded content. */
  min-height: calc(100vh - 64px);
}

@media (max-width: 900px) {
  .florilegium-shell { grid-template-columns: 1fr; }
}

/* INDEX (left sidebar) — sticky on desktop so it stays visible while
   the right pane scrolls; on mobile it just flows in document order. */
.florilegium-index {
  border-right: 1px solid var(--cp-gold-faint);
  display: flex;
  flex-direction: column;
}
@media (min-width: 901px) {
  .florilegium-index {
    position: sticky;
    top: 64px;
    height: calc(100vh - 64px);
    overflow: hidden;
  }
}

.index-head {
  padding: 24px 24px 16px;
  flex-shrink: 0;
}

.masthead-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 8px;
}
.folio-label {
  font-family: Georgia, serif;
  font-style: italic;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-size: 10px;
}
.index-count {
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
  letter-spacing: 0.05em;
}
.index-count em {
  color: var(--cp-ink);
  font-style: normal;
  font-variant-numeric: tabular-nums;
}

.index-title {
  font-family: Georgia, serif;
  font-size: 28px;
  font-weight: 400;
  letter-spacing: 0.02em;
  color: var(--cp-ink);
  margin: 0 0 18px;
}

.search-row {
  position: relative;
  margin-bottom: 12px;
}
.search-glyph {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  color: var(--cp-gold-soft);
  font-size: 14px;
}
.folio-input {
  width: 100%;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--cp-gold-faint);
  padding: 6px 6px 6px 22px;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 14px;
  color: var(--cp-ink);
  letter-spacing: 0.02em;
  transition: border-color 200ms;
}
.folio-input::placeholder { color: var(--cp-ink-mute); font-style: italic; }
.folio-input:focus {
  outline: none;
  border-bottom-color: var(--cp-gold);
}

.filter-row {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.folio-select {
  flex: 1;
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink);
  font-family: Georgia, serif;
  font-size: 12px;
  font-style: italic;
  padding: 4px 22px 4px 8px;
  cursor: pointer;
  appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--cp-gold-soft) 50%);
  background-position: right 8px center;
  background-size: 6px 6px;
  background-repeat: no-repeat;
  letter-spacing: 0.04em;
  min-width: 0;
}
.folio-select:hover { border-color: var(--cp-gold-soft); }
.folio-select:focus { outline: none; border-color: var(--cp-gold); }
.folio-select option {
  background: #14110a;
  color: var(--cp-ink);
}

.quality-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.quality-label {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.folio-slider {
  flex-grow: 1;
  accent-color: var(--cp-gold);
}
.quality-value {
  font-family: Georgia, serif;
  font-style: italic;
  font-variant-numeric: tabular-nums;
  font-size: 11px;
  color: var(--cp-gold);
  min-width: 30px;
  text-align: right;
}

.index-rule {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--cp-gold-soft), transparent);
  flex-shrink: 0;
}

.index-list {
  padding: 4px 12px 32px;
}
@media (min-width: 901px) {
  .index-list {
    flex-grow: 1;
    overflow-y: auto;
    padding-bottom: 80px;  /* breathing room past the last entry */
  }
}

.card-skeleton {
  height: 76px;
  margin: 8px 0;
  background: linear-gradient(
    90deg, rgba(200, 169, 110, 0.04) 25%,
    rgba(200, 169, 110, 0.08) 50%,
    rgba(200, 169, 110, 0.04) 75%
  );
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.index-empty, .page-empty, .page-loading {
  text-align: center;
  padding: 48px 16px;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
}
.empty-mark {
  display: block;
  font-size: 22px;
  color: var(--cp-gold-soft);
  margin-bottom: 12px;
}
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }
.loading-text { font-size: 13px; margin: 8px 0 0; letter-spacing: 0.05em; }

/* PAGE (right) — body scrolls on mobile, internal scroll on desktop */
.florilegium-page-right {
  padding: 0;
}
@media (min-width: 901px) {
  .florilegium-page-right { overflow-y: auto; }
}

.page-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
}
.page-empty .ornament {
  font-size: 36px;
  color: var(--cp-gold-soft);
  margin-bottom: 16px;
  display: block;
}
.empty-title {
  font-family: Georgia, serif;
  font-size: 24px;
  font-weight: 400;
  font-style: italic;
  color: var(--cp-ink);
  margin: 0 0 8px;
  letter-spacing: 0.02em;
}
.empty-tagline {
  font-style: italic;
  color: var(--cp-ink-mute);
  margin: 0;
  font-size: 14px;
}

/* LOAD MORE */
.load-more-row {
  text-align: center;
  padding: 18px 0 24px;
}
.folio-button {
  background: transparent;
  border: 1px solid var(--cp-gold-soft);
  color: var(--cp-gold);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  letter-spacing: 0.1em;
  padding: 8px 24px;
  cursor: pointer;
  transition: all 200ms;
}
.folio-button:hover:not(:disabled) {
  background: rgba(200, 169, 110, 0.08);
  border-color: var(--cp-gold);
}
.folio-button:disabled { opacity: 0.6; cursor: wait; }
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }
.end-of-list {
  text-align: center;
  padding: 24px 0 32px;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
  font-size: 13px;
}
.end-of-list .ornament-sm {
  display: block;
  font-size: 18px;
  color: var(--cp-gold-soft);
  margin-bottom: 8px;
}
.end-of-list p { margin: 0; }

/* MOBILE — single column. Both panes flow in document order so body
   scroll reveals everything; no internal-overflow trap. */
@media (max-width: 900px) {
  .florilegium-shell {
    grid-template-columns: 1fr;
    height: auto;
    min-height: 0;
  }
  .florilegium-index {
    border-right: none;
    border-bottom: 1px solid var(--cp-gold-faint);
    height: auto;
    max-height: none;
    overflow: visible;
  }
  .index-head { padding: 16px 14px 12px; }
  .index-title { font-size: 22px; }
  .filter-row { flex-direction: column; gap: 8px; }
  .index-list {
    flex-grow: 0;
    overflow: visible;
    padding: 4px 12px 24px;
  }
  .florilegium-page-right {
    overflow: visible;
    padding-bottom: 32px;
  }
  .page-empty { padding: 32px 16px; }
  .empty-title { font-size: 18px; }
}
</style>
