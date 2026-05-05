<template>
  <div class="annals-page">
    <div class="annals-shell">

      <!-- MASTHEAD -->
      <header class="annals-masthead">
        <div class="masthead-rule" />
        <div class="masthead-inner">
          <div class="masthead-row">
            <span class="folio-label">Annales · Chronicle</span>
            <div v-if="queueInfo" class="queue-indicator" :class="queueIndicatorClass">
              <span class="queue-mark">❦</span>
              queue · <em>{{ queueInfo.depth }}</em>
              <span v-if="queueInfo.stale_processing > 0">· stale <em>{{ queueInfo.stale_processing }}</em></span>
            </div>
          </div>
          <h1 class="annals-title">The Annals</h1>
          <p class="annals-tagline">A daily record of all that has been read, written, distilled into memory.</p>
        </div>
        <div class="masthead-rule" />
      </header>

      <!-- FILTERS -->
      <div class="annals-controls">
        <label class="ctl">
          <span class="ctl-label">Category</span>
          <select v-model="categoryFilter" class="folio-select">
            <option :value="null">All</option>
            <option v-for="c in categoryOptions" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="ctl">
          <span class="ctl-label">Instrument</span>
          <select v-model="machineFilter" class="folio-select">
            <option :value="null">All</option>
            <option v-for="m in machineOptions" :key="m" :value="m">{{ m }}</option>
          </select>
        </label>
      </div>

      <!-- LOADING -->
      <div v-if="loading" class="annals-loading">
        <span class="dotty">·  ·  ·</span>
        <p class="loading-text">consulting the chronicle</p>
      </div>

      <!-- ANNALS -->
      <template v-else-if="groupedMemories.length">
        <section
          v-for="group in groupedMemories"
          :key="group.date"
          class="annal-day"
        >
          <div class="day-header">
            <h2 class="day-title">{{ formatDayTitle(group.date) }}</h2>
            <span class="day-rule" />
            <span class="day-count">
              <em>{{ group.items.length }}</em> {{ group.items.length === 1 ? 'memory' : 'memories' }}
            </span>
          </div>

          <TransitionGroup name="crystallize" tag="div" class="day-entries">
            <article
              v-for="entry in group.items"
              :key="entry.key"
              class="annal-entry"
              :class="{
                'entry-ghost': entry.queue_status === 'pending',
                'entry-rejected': entry.queue_status === 'failed',
                'entry-expanded': expandedIds.has(entry.key),
                'entry-chunked': entry.isChunkGroup,
              }"
              @click="toggleExpand(entry.key)"
            >
              <!-- Marginalia: time -->
              <div class="entry-margin">
                <span v-if="entry.queue_status === 'pending'" class="margin-glyph pending">~</span>
                <span v-else-if="entry.queue_status === 'failed'" class="margin-glyph failed">✗</span>
                <span v-else-if="entry.isChunkGroup" class="margin-glyph chunked">¶</span>
                <span v-else class="margin-glyph dot">·</span>
                <span class="margin-time">
                  {{ formatTime(entry.created_at) }}
                </span>
              </div>

              <!-- Body -->
              <div class="entry-body">
                <h3 class="entry-title" :class="{ ghost: entry.queue_status }">
                  {{ entry.isChunkGroup
                    ? (entry.chunks[0].metadata?.document_title || entry.summary || entry.content.slice(0, 200))
                    : (entry.summary || entry.content.slice(0, 200) + (entry.content.length > 200 ? '…' : '')) }}
                </h3>

                <p
                  v-if="!expandedIds.has(entry.key)"
                  class="entry-preview"
                  :class="{ ghost: entry.queue_status }"
                >
                  {{ currentPageContent(entry).slice(0, 220) }}{{ currentPageContent(entry).length > 220 ? '…' : '' }}
                </p>
                <div v-else class="entry-expanded" :class="{ ghost: entry.queue_status }">
                  {{ currentPageContent(entry) }}
                </div>

                <!-- Page navigator for chunk groups -->
                <div v-if="entry.isChunkGroup && entry.chunks.length > 1" class="page-nav" @click.stop>
                  <span class="page-nav-label">{{ entry.chunks.length }} pages</span>
                  <button
                    v-for="(chunk, i) in entry.chunks"
                    :key="chunk.id"
                    class="page-btn"
                    :class="{ active: (activePages.get(entry.key) ?? 0) === i }"
                    @click.stop="setPage(entry.key, i)"
                  >{{ toRoman(i + 1).toLowerCase() }}</button>
                </div>
                <div v-else-if="entry.isChunkGroup" class="page-nav">
                  <span class="page-nav-label">i of {{ entry.chunks[0].metadata?.chunk_total ?? 1 }} pages</span>
                </div>

                <!-- Meta row -->
                <div class="entry-meta">
                  <span v-if="entry.category" class="meta-tag">{{ entry.category }}</span>
                  <span v-if="entry.source_machine" class="meta-tag muted">{{ entry.source_machine }}</span>

                  <span
                    v-if="entry.queue_status === 'pending'"
                    class="state-mark drafting"
                    :title="`Awaiting extraction${entry.attempts ? ` (retry ${entry.attempts}/${entry.max_attempts})` : ''}`"
                  >
                    <span class="state-dots"><i /><i /><i /></span>
                    drafting
                  </span>
                  <span
                    v-else-if="entry.queue_status === 'failed'"
                    class="state-mark rejected"
                    :title="entry.error_message || 'Write pipeline failed after max retries'"
                  >
                    rejected · {{ entry.attempts }}/{{ entry.max_attempts }}
                    <button
                      class="retry-btn"
                      :disabled="retryingIds.has(entry.key)"
                      title="Retry storing this memory"
                      @click.stop="retryQueue(entry)"
                    >
                      <span :class="{ spin: retryingIds.has(entry.key) }">↻</span>
                    </button>
                  </span>

                  <span class="meta-spacer" />
                  <span v-if="!entry.queue_status && entry.importance > 0" class="importance-bar"
                    :title="`importance ${(entry.importance * 100).toFixed(0)}%`">
                    <span class="importance-fill" :style="{ width: (entry.importance * 100) + '%' }" />
                  </span>
                  <span class="entry-chev">{{ expandedIds.has(entry.key) ? '↑' : '↓' }}</span>
                </div>
              </div>
            </article>
          </TransitionGroup>
        </section>
      </template>

      <div v-else class="annals-empty">
        <span class="ornament">❦</span>
        <p>No memories in this window of the chronicle.</p>
      </div>

      <div v-if="hasMore && !loading" class="load-more">
        <button class="folio-button" :disabled="loadingMore" @click="loadMore">
          <span v-if="loadingMore" class="dotty">·  ·  ·</span>
          <span v-else>turn the page</span>
        </button>
      </div>
    </div>
  </div>
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

const expandedIds = ref<Set<string>>(new Set())
const retryingIds = ref<Set<string>>(new Set())

async function retryQueue(entry: TimelineEntry) {
  const queueId = entry.key
  retryingIds.value.add(entry.key)
  retryingIds.value = new Set(retryingIds.value)
  try {
    await fetch(`/api/queue/${queueId}/retry`, { method: 'POST' })
    setTimeout(() => fetchTimeline({ silent: true }), 1500)
  } finally {
    retryingIds.value.delete(entry.key)
    retryingIds.value = new Set(retryingIds.value)
  }
}

const activePages = ref<Map<string, number>>(new Map())

function toggleExpand(key: string) {
  if (expandedIds.value.has(key)) {
    expandedIds.value.delete(key)
  } else {
    expandedIds.value.add(key)
  }
  expandedIds.value = new Set(expandedIds.value)
}

function setPage(key: string, page: number) {
  activePages.value.set(key, page)
  activePages.value = new Map(activePages.value)
  if (!expandedIds.value.has(key)) {
    expandedIds.value.add(key)
    expandedIds.value = new Set(expandedIds.value)
  }
}

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
  chunks: Memory[]
}

function currentPageContent(entry: TimelineEntry): string {
  if (!entry.isChunkGroup) return entry.content
  const page = activePages.value.get(entry.key) ?? 0
  return entry.chunks[page]?.content ?? entry.content
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit',
  })
}

function formatDayTitle(date: string): string {
  // Input is e.g. "Friday, May 5, 2026" — return as-is, the styles handle the small-caps
  return date
}

function toRoman(n: number): string {
  const r: [number, string][] = [
    [50, 'L'], [40, 'XL'], [10, 'X'], [9, 'IX'],
    [5, 'V'], [4, 'IV'], [1, 'I'],
  ]
  let out = ''
  for (const [v, s] of r) { while (n >= v) { out += s; n -= v } }
  return out
}

const categoryOptions = computed(() => {
  if (!statsStore.stats) return []
  return statsStore.stats.by_category.map(c => c.category)
})

const machineOptions = computed(() => {
  if (!statsStore.stats) return []
  return statsStore.stats.by_machine.map(m => m.source_machine)
})

const groupedMemories = computed(() => {
  const queueItems = memories.value.filter(m => m.queue_status)
  const stored = memories.value.filter(m => !m.queue_status)

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
  for (const chunks of docGroups.values()) {
    chunks.sort((a, b) => (a.metadata?.chunk_index ?? 0) - (b.metadata?.chunk_index ?? 0))
  }

  const entries: TimelineEntry[] = []
  for (const mem of queueItems) {
    entries.push({
      key: mem.id, created_at: mem.created_at,
      summary: mem.summary, content: mem.content,
      category: mem.category, source_machine: mem.source_machine,
      importance: mem.importance,
      queue_status: mem.queue_status, attempts: mem.attempts,
      max_attempts: mem.max_attempts, error_message: mem.error_message,
      isChunkGroup: false, chunks: [mem],
    })
  }
  for (const [docId, chunks] of docGroups) {
    const first = chunks[0]
    entries.push({
      key: `doc:${docId}`, created_at: first.created_at,
      summary: first.metadata?.document_title as string ?? first.summary,
      content: first.content,
      category: first.category, source_machine: first.source_machine,
      importance: first.importance,
      isChunkGroup: true, chunks,
    })
  }
  for (const mem of standalone) {
    entries.push({
      key: mem.id, created_at: mem.created_at,
      summary: mem.summary, content: mem.content,
      category: mem.category, source_machine: mem.source_machine,
      importance: mem.importance,
      isChunkGroup: false, chunks: [mem],
    })
  }
  entries.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())

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

interface QueueInfo { depth: number; stale_processing: number }
const queueInfo = ref<QueueInfo | null>(null)
let queueTimer: ReturnType<typeof setInterval> | null = null

async function fetchQueue() {
  if (document.visibilityState !== 'visible') return
  try {
    const r = await fetch('/api/health/detailed')
    if (!r.ok) return
    const d = await r.json()
    if (d?.write_queue) queueInfo.value = d.write_queue as QueueInfo
  } catch { /* transient — next tick will retry */ }
}

const queueIndicatorClass = computed(() => {
  const q = queueInfo.value
  if (!q) return ''
  if (q.stale_processing > 0) return 'queue-error'
  if (q.depth > 100) return 'queue-warn'
  if (q.depth > 0) return 'queue-active'
  return 'queue-clear'
})

onMounted(async () => {
  await statsStore.fetchStats()
  fetchTimeline()
  startPoll()
  fetchQueue()
  queueTimer = setInterval(fetchQueue, 15000)
})
onUnmounted(() => {
  stopPoll()
  if (queueTimer) { clearInterval(queueTimer); queueTimer = null }
})
</script>

<style scoped>
.annals-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  --cp-bg: rgba(18, 14, 8, 0.4);
  --cp-bg-deep: rgba(14, 11, 6, 0.55);
  --rejected: #c47a6a;
  --pending: #c89e6e;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 32px 24px 80px;
  min-height: 100vh;
}

.annals-shell {
  max-width: 820px;
  margin: 0 auto;
}

/* MASTHEAD */
.annals-masthead { margin-bottom: 32px; }
.masthead-rule {
  height: 1px;
  background: linear-gradient(
    90deg, transparent, var(--cp-gold-soft) 30%,
    var(--cp-gold) 50%, var(--cp-gold-soft) 70%, transparent
  );
}
.masthead-inner { padding: 16px 0 12px; text-align: center; }
.masthead-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
  margin-bottom: 12px;
}
.folio-label {
  font-style: italic;
  letter-spacing: 0.22em;
  color: var(--cp-gold);
  font-size: 10px;
}
.queue-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-style: italic;
  letter-spacing: 0.05em;
  color: var(--cp-ink-mute);
}
.queue-indicator em {
  color: var(--cp-ink);
  font-style: normal;
  font-variant-numeric: tabular-nums;
}
.queue-mark { color: var(--cp-gold-soft); font-size: 12px; }
.queue-active .queue-mark { color: var(--cp-gold); }
.queue-warn .queue-mark { color: var(--pending); }
.queue-error .queue-mark { color: var(--rejected); }
.queue-error em { color: var(--rejected); }

.annals-title {
  font-family: Georgia, serif;
  font-size: clamp(34px, 4.5vw, 48px);
  font-weight: 400;
  letter-spacing: 0.02em;
  margin: 0 0 4px;
  color: var(--cp-ink);
}
.annals-tagline {
  font-style: italic;
  color: var(--cp-ink-mute);
  font-size: 14px;
  margin: 0 0 16px;
}

/* CONTROLS */
.annals-controls {
  display: flex;
  gap: 24px;
  margin-bottom: 32px;
  padding: 12px 0;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.ctl {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ctl-label {
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
}
.folio-select {
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink);
  font-family: Georgia, serif;
  font-size: 13px;
  padding: 4px 22px 4px 10px;
  cursor: pointer;
  appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--cp-gold-soft) 50%);
  background-position: right 8px center;
  background-size: 6px 6px;
  background-repeat: no-repeat;
  letter-spacing: 0.04em;
}
.folio-select:hover { border-color: var(--cp-gold-soft); }
.folio-select:focus { outline: none; border-color: var(--cp-gold); }
.folio-select option {
  background: #14110a;
  color: var(--cp-ink);
}

/* ANNAL DAY */
.annal-day {
  margin-bottom: 56px;
}
.day-header {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 16px;
  align-items: baseline;
  margin-bottom: 20px;
}
.day-title {
  font-family: Georgia, serif;
  font-size: 13px;
  font-weight: 400;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-style: italic;
  margin: 0;
}
.day-rule {
  height: 1px;
  background: linear-gradient(
    90deg, var(--cp-gold-soft), transparent
  );
  align-self: center;
}
.day-count {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink-mute);
  letter-spacing: 0.05em;
}
.day-count em {
  color: var(--cp-ink);
  font-style: normal;
  font-variant-numeric: tabular-nums;
}

/* ANNAL ENTRY */
.day-entries { display: flex; flex-direction: column; }
.annal-entry {
  display: grid;
  grid-template-columns: 76px 1fr;
  gap: 16px;
  padding: 16px 8px;
  border-bottom: 1px dotted var(--cp-gold-faint);
  cursor: pointer;
  transition: all 200ms cubic-bezier(0.22, 1, 0.36, 1);
}
.annal-entry:hover {
  padding-left: 12px;
  background: rgba(200, 169, 110, 0.025);
}
.annal-entry.entry-expanded {
  background: rgba(200, 169, 110, 0.04);
}
.annal-entry.entry-ghost {
  opacity: 0.85;
}
.annal-entry.entry-rejected {
  background: rgba(196, 122, 106, 0.04);
  border-bottom-color: rgba(196, 122, 106, 0.15);
}

/* MARGINALIA */
.entry-margin {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  text-align: right;
  padding-right: 12px;
  border-right: 1px solid var(--cp-gold-faint);
  font-family: Georgia, serif;
}
.margin-glyph {
  font-family: Georgia, serif;
  font-size: 18px;
  line-height: 1;
  color: var(--cp-gold-soft);
}
.margin-glyph.dot { color: var(--cp-gold-soft); }
.margin-glyph.chunked { color: var(--cp-gold); font-style: italic; }
.margin-glyph.pending { color: var(--pending); }
.margin-glyph.failed { color: var(--rejected); }
.margin-time {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  font-style: italic;
  color: var(--cp-ink-mute);
  letter-spacing: 0.05em;
}

/* ENTRY BODY */
.entry-body {
  min-width: 0;
}
.entry-title {
  font-family: Georgia, serif;
  font-size: 15px;
  font-weight: 400;
  line-height: 1.5;
  color: var(--cp-ink);
  margin: 0 0 6px;
}
.entry-title.ghost { color: var(--cp-ink-mute); font-style: italic; }
.entry-preview {
  font-family: Georgia, serif;
  font-size: 13px;
  line-height: 1.6;
  color: var(--cp-ink-mute);
  margin: 0 0 10px;
}
.entry-expanded {
  font-family: Georgia, serif;
  font-size: 14px;
  line-height: 1.7;
  color: var(--cp-ink);
  margin: 0 0 12px;
  padding: 12px 16px;
  background: rgba(200, 169, 110, 0.04);
  border-left: 2px solid var(--cp-gold-soft);
  white-space: pre-wrap;
}
.entry-expanded.ghost, .entry-preview.ghost { color: var(--cp-ink-mute); }

/* PAGE NAV (chunks) */
.page-nav {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin: 8px 0 10px;
  padding: 6px 0;
  border-top: 1px dotted var(--cp-gold-faint);
}
.page-nav-label {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
  margin-right: 8px;
}
.page-btn {
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink-mute);
  font-family: Georgia, serif;
  font-size: 11px;
  font-style: italic;
  padding: 2px 7px;
  min-width: 24px;
  cursor: pointer;
  letter-spacing: 0.05em;
  transition: all 150ms;
}
.page-btn:hover { border-color: var(--cp-gold-soft); color: var(--cp-ink); }
.page-btn.active {
  border-color: var(--cp-gold);
  color: var(--cp-gold);
  background: rgba(200, 169, 110, 0.1);
}

/* META ROW */
.entry-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  font-family: Georgia, serif;
}
.meta-tag {
  font-size: 11px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-style: italic;
}
.meta-tag.muted { color: var(--cp-ink-mute); }
.meta-spacer { flex-grow: 1; }

/* STATE MARKS */
.state-mark {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  letter-spacing: 0.1em;
  font-style: italic;
}
.state-mark.drafting { color: var(--pending); }
.state-mark.rejected { color: var(--rejected); }
.state-dots {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.state-dots i {
  display: inline-block;
  width: 4px; height: 4px;
  background: var(--pending);
  border-radius: 50%;
  animation: pulse-dot 1.4s ease-in-out infinite;
}
.state-dots i:nth-child(2) { animation-delay: 0.2s; }
.state-dots i:nth-child(3) { animation-delay: 0.4s; }
@keyframes pulse-dot {
  0%, 80%, 100% { opacity: 0.3; }
  40% { opacity: 1; }
}

.retry-btn {
  background: transparent;
  border: 1px solid rgba(196, 122, 106, 0.4);
  color: var(--rejected);
  width: 22px; height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-left: 4px;
  cursor: pointer;
  font-size: 14px;
  transition: all 150ms;
}
.retry-btn:hover:not(:disabled) {
  border-color: var(--rejected);
  background: rgba(196, 122, 106, 0.1);
}
.retry-btn:disabled { opacity: 0.5; cursor: wait; }
.spin { display: inline-block; animation: spin-slow 1s linear infinite; }
@keyframes spin-slow { to { transform: rotate(360deg); } }

/* IMPORTANCE */
.importance-bar {
  display: inline-block;
  width: 50px;
  height: 2px;
  background: rgba(200, 169, 110, 0.1);
  position: relative;
}
.importance-fill {
  display: block;
  height: 100%;
  background: var(--cp-gold);
}
.entry-chev {
  font-family: Georgia, serif;
  color: var(--cp-gold-soft);
  font-size: 12px;
  font-style: italic;
}

/* TRANSITIONS */
.crystallize-enter-active { transition: all 350ms cubic-bezier(0.22, 1, 0.36, 1); }
.crystallize-enter-from { opacity: 0; transform: translateY(-6px); }
.crystallize-leave-active { transition: all 200ms ease; }
.crystallize-leave-to { opacity: 0; }

/* STATES */
.annals-loading, .annals-empty {
  text-align: center;
  padding: 80px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
}
.annals-empty .ornament {
  display: block;
  font-size: 24px;
  color: var(--cp-gold-soft);
  margin-bottom: 12px;
}
.loading-text { font-size: 13px; margin: 8px 0 0; letter-spacing: 0.05em; }
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }

/* LOAD MORE */
.load-more {
  text-align: center;
  margin: 32px 0;
}
.folio-button {
  background: transparent;
  border: 1px solid var(--cp-gold-soft);
  color: var(--cp-gold);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  letter-spacing: 0.1em;
  padding: 10px 28px;
  cursor: pointer;
  transition: all 200ms;
}
.folio-button:hover:not(:disabled) {
  background: rgba(200, 169, 110, 0.08);
  border-color: var(--cp-gold);
}
.folio-button:disabled { opacity: 0.6; cursor: wait; }
</style>
