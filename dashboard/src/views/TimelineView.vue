<template>
  <v-container fluid style="max-width: 900px;">
    <!-- Filter Bar -->
    <div class="d-flex ga-3 mb-5 align-center">
      <v-select
        v-model="categoryFilter"
        :items="categoryOptions"
        label="Category"
        clearable
        style="max-width: 200px;"
      />
      <v-select
        v-model="machineFilter"
        :items="machineOptions"
        label="Machine"
        clearable
        style="max-width: 200px;"
      />
    </div>

    <!-- Loading -->
    <template v-if="loading">
      <div v-for="n in 3" :key="n" class="skeleton-block mb-4" />
    </template>

    <!-- Timeline -->
    <template v-else-if="groupedMemories.length">
      <div v-for="group in groupedMemories" :key="group.date" class="mb-8">
        <div class="d-flex align-center mb-4">
          <div class="timeline-date-pill">{{ group.date }}</div>
          <div class="timeline-line" />
        </div>

        <div class="timeline-entries">
          <TransitionGroup name="crystallize">
          <div
            v-for="entry in group.items"
            :key="entry.key"
            class="timeline-entry d-flex ga-3 mb-3"
            :class="{
              'timeline-entry--ghost': entry.queue_status === 'pending',
              'timeline-entry--rejected': entry.queue_status === 'failed',
            }"
          >
            <!-- Time -->
            <div
              class="text-caption pt-1"
              :class="entry.queue_status ? 'text-warning' : 'text-medium-emphasis'"
              style="min-width: 56px; font-variant-numeric: tabular-nums;"
            >
              <span v-if="entry.queue_status === 'pending'" class="ghost-tilde">~</span>
              <span v-if="entry.queue_status === 'failed'" class="ghost-tilde">×</span>
              {{ new Date(entry.created_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) }}
            </div>

            <!-- Dot + line -->
            <div class="d-flex flex-column align-center" style="width: 12px;">
              <div
                class="timeline-dot"
                :class="{
                  'timeline-dot--pending': entry.queue_status === 'pending',
                  'timeline-dot--failed': entry.queue_status === 'failed',
                  'timeline-dot--chunked': entry.isChunkGroup,
                }"
              />
              <div
                class="timeline-stem"
                :class="{ 'timeline-stem--flow': entry.queue_status === 'pending' }"
              />
            </div>

            <!-- Card -->
            <v-card
              variant="flat"
              class="flex-grow-1 timeline-card"
              :class="{
                'timeline-card--ghost': entry.queue_status === 'pending',
                'timeline-card--rejected': entry.queue_status === 'failed',
                'timeline-card--expanded': expandedIds.has(entry.key),
                'timeline-card--clickable': true,
              }"
              @click="toggleExpand(entry.key)"
            >
              <v-card-text class="pa-3">
                <!-- Header row -->
                <div class="d-flex align-start ga-2 mb-1">
                  <div
                    class="text-body-2 font-weight-medium flex-grow-1"
                    :class="{ 'ghost-text': entry.queue_status }"
                    style="line-height: 1.5;"
                  >
                    {{ entry.isChunkGroup ? (entry.chunks[0].metadata?.document_title || entry.summary || entry.content.slice(0, 200)) : (entry.summary || entry.content.slice(0, 200) + (entry.content.length > 200 ? '…' : '')) }}
                  </div>
                  <v-icon
                    :icon="expandedIds.has(entry.key) ? 'mdi-chevron-up' : 'mdi-chevron-down'"
                    size="16"
                    class="expand-icon mt-1"
                    style="opacity: 0.4; flex-shrink: 0;"
                  />
                </div>

                <!-- Preview (collapsed) -->
                <div
                  v-if="!expandedIds.has(entry.key)"
                  class="text-body-2 text-medium-emphasis mb-2"
                  :class="{ 'ghost-text': entry.queue_status }"
                  style="line-height: 1.5;"
                >
                  {{ currentPageContent(entry).slice(0, 180) }}{{ currentPageContent(entry).length > 180 ? '…' : '' }}
                </div>

                <!-- Expanded full content -->
                <div
                  v-else
                  class="expanded-content mb-2"
                  :class="{ 'ghost-text': entry.queue_status }"
                >
                  <div class="content-prose">{{ currentPageContent(entry) }}</div>
                </div>

                <!-- Chunk page navigator -->
                <div v-if="entry.isChunkGroup && entry.chunks.length > 1" class="page-nav mb-2" @click.stop>
                  <span class="page-nav__label">{{ entry.chunks.length }} pages</span>
                  <button
                    v-for="(chunk, i) in entry.chunks"
                    :key="chunk.id"
                    class="page-nav__btn"
                    :class="{ 'page-nav__btn--active': (activePages.get(entry.key) ?? 0) === i }"
                    @click.stop="setPage(entry.key, i)"
                  >{{ i + 1 }}</button>
                </div>
                <div v-else-if="entry.isChunkGroup" class="page-nav mb-2">
                  <span class="page-nav__label">1 of {{ entry.chunks[0].metadata?.chunk_total ?? 1 }} pages loaded</span>
                </div>

                <!-- Meta row -->
                <div class="d-flex ga-2 align-center flex-wrap">
                  <v-chip v-if="entry.category" size="x-small" variant="tonal" color="primary" class="font-weight-medium">
                    {{ entry.category }}
                  </v-chip>
                  <v-chip v-if="entry.source_machine" size="x-small" variant="tonal" color="secondary" class="font-weight-medium">
                    {{ entry.source_machine }}
                  </v-chip>

                  <!-- Queue status marker -->
                  <span
                    v-if="entry.queue_status === 'pending'"
                    class="queue-marker queue-marker--pending"
                    :title="`Awaiting extraction${entry.attempts ? ` (retry ${entry.attempts}/${entry.max_attempts})` : ''}`"
                  >
                    <span class="queue-marker__dots"><i /><i /><i /></span>
                    drafting
                  </span>
                  <span
                    v-else-if="entry.queue_status === 'failed'"
                    class="queue-marker queue-marker--failed"
                    :title="entry.error_message || 'Write pipeline failed after max retries — memory was not stored'"
                  >
                    <v-icon icon="mdi-alert-circle-outline" size="12" class="mr-1" />
                    rejected · {{ entry.attempts }}/{{ entry.max_attempts }}
                    <button
                      class="retry-btn"
                      :disabled="retryingIds.has(entry.key)"
                      :title="'Retry storing this memory'"
                      @click.stop="retryQueue(entry)"
                    >
                      <v-icon :icon="retryingIds.has(entry.key) ? 'mdi-loading' : 'mdi-refresh'" size="11" :class="{ 'spin': retryingIds.has(entry.key) }" />
                    </button>
                  </span>

                  <v-spacer />
                  <div v-if="!entry.queue_status && entry.importance > 0" class="d-flex align-center ga-1">
                    <v-progress-linear
                      :model-value="entry.importance * 100"
                      color="warning"
                      height="3"
                      rounded
                      style="width: 40px;"
                    />
                  </div>
                </div>
              </v-card-text>
            </v-card>
          </div>
          </TransitionGroup>
        </div>
      </div>
    </template>

    <div v-else class="text-center text-medium-emphasis pa-12">
      <v-icon icon="mdi-timeline-clock-outline" size="40" class="mb-2 d-block mx-auto" style="opacity: 0.2;" />
      No memories found
    </div>

    <!-- Load More -->
    <div v-if="hasMore && !loading" class="text-center mt-4 mb-6">
      <v-btn variant="tonal" color="primary" :loading="loadingMore" @click="loadMore">
        Load more
      </v-btn>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { useTimeline } from '@/composables/useTimeline'
import { useStatsStore } from '@/stores/stats'
import { useSSE } from '@/composables/useSSE'
import type { Memory } from '@/types'

const statsStore = useStatsStore()
const {
  memories,
  loading,
  loadingMore,
  hasMore,
  categoryFilter,
  machineFilter,
  fetchTimeline,
  loadMore,
} = useTimeline()

// Expand state — track by entry key
const expandedIds = ref<Set<string>>(new Set())
// Retry in-progress tracking
const retryingIds = ref<Set<string>>(new Set())

async function retryQueue(entry: TimelineEntry) {
  const queueId = entry.key  // for queue items key === memory id (queue row id)
  retryingIds.value.add(entry.key)
  retryingIds.value = new Set(retryingIds.value)
  try {
    await fetch(`/api/queue/${queueId}/retry`, { method: 'POST' })
    // Refresh timeline after short delay so worker has time to claim it
    setTimeout(() => fetchTimeline({ silent: true }), 1500)
  } finally {
    retryingIds.value.delete(entry.key)
    retryingIds.value = new Set(retryingIds.value)
  }
}
// Page state for chunk groups — entry key → chunk index
const activePages = ref<Map<string, number>>(new Map())

function toggleExpand(key: string) {
  if (expandedIds.value.has(key)) {
    expandedIds.value.delete(key)
  } else {
    expandedIds.value.add(key)
  }
  // trigger reactivity
  expandedIds.value = new Set(expandedIds.value)
}

function setPage(key: string, page: number) {
  activePages.value.set(key, page)
  activePages.value = new Map(activePages.value)
  // auto-expand when navigating pages
  if (!expandedIds.value.has(key)) {
    expandedIds.value.add(key)
    expandedIds.value = new Set(expandedIds.value)
  }
}

// ─── Entry shape ──────────────────────────────────────────────────────────────
interface TimelineEntry {
  key: string
  created_at: string
  summary: string | null
  content: string
  category: string | null
  source_machine: string | null
  importance: number
  queue_status?: 'pending' | 'failed'
  attempts?: number
  max_attempts?: number
  error_message?: string | null
  isChunkGroup: boolean
  chunks: Memory[]    // for chunk groups: all loaded chunks sorted by index
}

function currentPageContent(entry: TimelineEntry): string {
  if (!entry.isChunkGroup) return entry.content
  const page = activePages.value.get(entry.key) ?? 0
  return entry.chunks[page]?.content ?? entry.content
}

// ─── Grouping ─────────────────────────────────────────────────────────────────
const categoryOptions = computed(() => {
  if (!statsStore.stats) return []
  return statsStore.stats.by_category.map(c => c.category)
})

const machineOptions = computed(() => {
  if (!statsStore.stats) return []
  return statsStore.stats.by_machine.map(m => m.source_machine)
})

const groupedMemories = computed(() => {
  // 1. Separate queue items (pending/failed) from stored memories
  const queueItems = memories.value.filter(m => m.queue_status)
  const stored = memories.value.filter(m => !m.queue_status)

  // 2. Group chunks by document_id
  const docGroups = new Map<string, Memory[]>()
  const standalone: Memory[] = []

  for (const mem of stored) {
    const docId = mem.metadata?.document_id
    if (docId) {
      if (!docGroups.has(docId)) docGroups.set(docId, [])
      docGroups.get(docId)!.push(mem)
    } else {
      standalone.push(mem)
    }
  }

  // Sort chunks within each doc group
  for (const chunks of docGroups.values()) {
    chunks.sort((a, b) => (a.metadata?.chunk_index ?? 0) - (b.metadata?.chunk_index ?? 0))
  }

  // 3. Build flat entry list
  const entries: TimelineEntry[] = []

  // Queue items as standalone entries
  for (const mem of queueItems) {
    entries.push({
      key: mem.id,
      created_at: mem.created_at,
      summary: mem.summary,
      content: mem.content,
      category: mem.category,
      source_machine: mem.source_machine,
      importance: mem.importance,
      queue_status: mem.queue_status,
      attempts: mem.attempts,
      max_attempts: mem.max_attempts,
      error_message: mem.error_message,
      isChunkGroup: false,
      chunks: [mem],
    })
  }

  // Chunk groups — represent as single entry, keyed by document_id
  for (const [docId, chunks] of docGroups) {
    const first = chunks[0]
    entries.push({
      key: `doc:${docId}`,
      created_at: first.created_at,
      summary: first.metadata?.document_title as string ?? first.summary,
      content: first.content,
      category: first.category,
      source_machine: first.source_machine,
      importance: first.importance,
      isChunkGroup: true,
      chunks,
    })
  }

  // Standalone stored memories
  for (const mem of standalone) {
    entries.push({
      key: mem.id,
      created_at: mem.created_at,
      summary: mem.summary,
      content: mem.content,
      category: mem.category,
      source_machine: mem.source_machine,
      importance: mem.importance,
      isChunkGroup: false,
      chunks: [mem],
    })
  }

  // 4. Sort all by created_at desc
  entries.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())

  // 5. Group by day
  const groups: Record<string, TimelineEntry[]> = {}
  for (const entry of entries) {
    const day = new Date(entry.created_at).toLocaleDateString(undefined, {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    })
    if (!groups[day]) groups[day] = []
    groups[day].push(entry)
  }
  return Object.entries(groups).map(([date, items]) => ({ date, items }))
})

useSSE((evt) => {
  if (['memory_created', 'memory_updated', 'memory_deleted'].includes(evt.type)) {
    fetchTimeline({ silent: true })
  }
})

watch([categoryFilter, machineFilter], () => { fetchTimeline() })

let pollTimer: ReturnType<typeof setInterval> | null = null
function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') fetchTimeline({ silent: true })
  }, 8000)
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

onMounted(async () => {
  await statsStore.fetchStats()
  fetchTimeline()
  startPoll()
})
onUnmounted(stopPoll)
</script>

<style scoped>
.timeline-date-pill {
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
  font-size: 0.82rem;
  padding: 4px 14px;
  border-radius: 8px;
  white-space: nowrap;
}
.timeline-line {
  flex-grow: 1;
  height: 1px;
  background: rgba(255, 255, 255, 0.06);
  margin-left: 12px;
}
.timeline-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(var(--v-theme-primary));
  flex-shrink: 0;
  margin-top: 6px;
  transition: background 320ms ease, box-shadow 320ms ease;
}
.timeline-dot--pending {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-warning), 0.7);
  animation: dot-breathe 2.6s ease-in-out infinite;
}
.timeline-dot--failed {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-error), 0.55);
}
.timeline-dot--chunked {
  background: rgba(var(--v-theme-primary), 0.5);
  box-shadow: 0 0 0 2px rgba(var(--v-theme-primary), 0.18);
}
.timeline-stem {
  width: 1px;
  flex-grow: 1;
  background: rgba(255, 255, 255, 0.06);
  min-height: 8px;
  position: relative;
  overflow: hidden;
}
.timeline-stem--flow {
  background: linear-gradient(
    to bottom,
    rgba(var(--v-theme-warning), 0) 0%,
    rgba(var(--v-theme-warning), 0.35) 50%,
    rgba(var(--v-theme-warning), 0) 100%
  );
  background-size: 100% 220%;
  animation: stem-flow 2.8s linear infinite;
}
.timeline-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
  transition: border-color 150ms ease, background-color 320ms ease, opacity 320ms ease;
}
.timeline-card--clickable {
  cursor: pointer;
}
.timeline-card--clickable:hover {
  border-color: rgba(255, 255, 255, 0.1);
}
.timeline-card--clickable:hover .expand-icon {
  opacity: 0.7 !important;
}
.timeline-card--expanded {
  border-color: rgba(var(--v-theme-primary), 0.2);
}

/* Expanded content */
.expanded-content {
  border-top: 1px solid rgba(255, 255, 255, 0.05);
  padding-top: 10px;
  margin-top: 4px;
}
.content-prose {
  font-size: 0.82rem;
  line-height: 1.7;
  color: rgba(var(--v-theme-on-surface), 0.85);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  max-height: 480px;
  overflow-y: auto;
  padding-right: 4px;
}
.content-prose::-webkit-scrollbar { width: 3px; }
.content-prose::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 2px; }

/* Page navigator */
.page-nav {
  display: flex;
  align-items: center;
  gap: 4px;
  padding-top: 6px;
}
.page-nav__label {
  font-size: 0.68rem;
  color: rgba(var(--v-theme-on-surface), 0.35);
  letter-spacing: 0.03em;
  margin-right: 4px;
  user-select: none;
}
.page-nav__btn {
  min-width: 22px;
  height: 22px;
  border-radius: 4px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.5);
  font-size: 0.7rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 120ms, border-color 120ms, color 120ms;
  line-height: 1;
  padding: 0 5px;
}
.page-nav__btn:hover {
  background: rgba(var(--v-theme-primary), 0.12);
  border-color: rgba(var(--v-theme-primary), 0.3);
  color: rgb(var(--v-theme-primary));
}
.page-nav__btn--active {
  background: rgba(var(--v-theme-primary), 0.18);
  border-color: rgba(var(--v-theme-primary), 0.4);
  color: rgb(var(--v-theme-primary));
}

/* Ghost: memory in amber, not yet crystallized */
.timeline-card--ghost {
  background-color: color-mix(in oklch, rgb(var(--v-theme-surface)) 94%, rgb(var(--v-theme-warning)));
  border: 1px dashed rgba(var(--v-theme-warning), 0.28);
  position: relative;
}
.timeline-card--ghost::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  border-radius: inherit;
  background: linear-gradient(
    110deg,
    transparent 0%, transparent 40%,
    rgba(var(--v-theme-warning), 0.06) 50%,
    transparent 60%, transparent 100%
  );
  background-size: 280% 100%;
  animation: ghost-sweep 4.5s linear infinite;
}
.timeline-entry--ghost .ghost-text {
  font-style: italic;
  color: color-mix(in oklch, rgb(var(--v-theme-on-surface)) 78%, transparent);
}

/* Rejected */
.timeline-card--rejected {
  background-color: color-mix(in oklch, rgb(var(--v-theme-surface)) 96%, rgb(var(--v-theme-error)));
  border: 1px solid rgba(var(--v-theme-error), 0.18);
  opacity: 0.82;
}
.timeline-entry--rejected .ghost-text {
  color: color-mix(in oklch, rgb(var(--v-theme-on-surface)) 70%, transparent);
  text-decoration: line-through;
  text-decoration-color: rgba(var(--v-theme-error), 0.35);
  text-decoration-thickness: 1px;
}

.ghost-tilde {
  display: inline-block;
  width: 0.7em;
  margin-right: 2px;
  opacity: 0.85;
}

/* Typographic queue marker */
.queue-marker {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.7rem;
  font-weight: 500;
  letter-spacing: 0.04em;
  text-transform: lowercase;
  font-variant-numeric: tabular-nums;
  padding: 0 4px;
  user-select: none;
}
.queue-marker--pending { color: rgb(var(--v-theme-warning)); }
.queue-marker--failed  { color: rgb(var(--v-theme-error)); }

.retry-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 3px;
  border: 1px solid rgba(var(--v-theme-error), 0.3);
  background: transparent;
  color: rgb(var(--v-theme-error));
  cursor: pointer;
  margin-left: 4px;
  opacity: 0.7;
  transition: opacity 120ms, background 120ms;
  vertical-align: middle;
}
.retry-btn:hover { opacity: 1; background: rgba(var(--v-theme-error), 0.1); }
.retry-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.spin { animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.queue-marker__dots {
  display: inline-flex;
  gap: 2px;
  align-items: center;
}
.queue-marker__dots i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.35;
  animation: dot-wave 1.4s ease-in-out infinite;
}
.queue-marker__dots i:nth-child(2) { animation-delay: 0.18s; }
.queue-marker__dots i:nth-child(3) { animation-delay: 0.36s; }

/* Crystallization transition */
.crystallize-enter-from { opacity: 0; transform: translateY(4px); }
.crystallize-enter-active,
.crystallize-leave-active { transition: opacity 340ms ease, transform 340ms ease; }
.crystallize-leave-to { opacity: 0; transform: translateY(-2px); }
.crystallize-move { transition: transform 420ms cubic-bezier(0.22, 0.61, 0.36, 1); }

@keyframes dot-breathe {
  0%, 100% { box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-warning), 0.35); }
  50%       { box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-warning), 0.85); }
}
@keyframes stem-flow {
  0%   { background-position: 0 100%; }
  100% { background-position: 0 -100%; }
}
@keyframes ghost-sweep {
  0%   { background-position: -50% 0; }
  100% { background-position: 150% 0; }
}
@keyframes dot-wave {
  0%, 100% { opacity: 0.25; transform: translateY(0); }
  50%       { opacity: 1;    transform: translateY(-1.5px); }
}
@media (prefers-reduced-motion: reduce) {
  .timeline-dot--pending,
  .timeline-stem--flow,
  .timeline-card--ghost::before,
  .queue-marker__dots i { animation: none; }
}
.skeleton-block {
  background: linear-gradient(90deg, rgb(var(--v-theme-surface)) 25%, rgba(255,255,255,0.03) 50%, rgb(var(--v-theme-surface)) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 12px;
  height: 120px;
}
@keyframes shimmer {
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
</style>
