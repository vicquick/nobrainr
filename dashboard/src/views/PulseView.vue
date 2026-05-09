<template>
  <div class="pulse-page">
    <div class="pulse-shell">

      <!-- MASTHEAD -->
      <header class="pulse-masthead">
        <div class="masthead-rule" />
        <div class="masthead-inner">
          <div class="masthead-row">
            <span class="folio-label">Liber Diurnus · Daybook</span>
            <div class="live-indicator">
              <span class="live-pulse" :class="{ active: pulsing }">❦</span>
              <span class="live-cd">refresh in {{ countdown }}″</span>
            </div>
          </div>
          <h1 class="pulse-title">The Scribe's Pulse</h1>
          <p class="pulse-tagline">A daybook of the works in progress — what is being read, written, distilled.</p>
        </div>
        <div class="masthead-rule" />
      </header>

      <!-- LOADING STATE -->
      <template v-if="!stats">
        <div class="loading-folio">
          <Dotty />
          <p class="loading-text">consulting the registry</p>
        </div>
      </template>

      <template v-else>

        <!-- I — EXTRACTION -->
        <section v-reveal class="quire">
          <div class="quire-head">
            <span class="quire-numeral">I.</span>
            <span class="quire-title">Of memories scribed</span>
            <span class="quire-flag" :class="{ active: rate !== null && rate > 0 }">
              <span v-if="rate !== null && rate > 0">{{ rate }} per minute</span>
              <span v-else>—</span>
            </span>
          </div>

          <div class="ledger-figures">
            <article class="ledger-line">
              <span class="ledger-key">In total, scribed</span>
              <span class="ledger-fig">{{ stats.total_memories.toLocaleString() }}</span>
            </article>
            <article class="ledger-line">
              <span class="ledger-key">Entities catalogued</span>
              <span class="ledger-fig">{{ stats.total_entities.toLocaleString() }}</span>
            </article>
            <article class="ledger-line">
              <span class="ledger-key">Relations drawn</span>
              <span class="ledger-fig">{{ stats.total_relations.toLocaleString() }}</span>
            </article>
            <article class="ledger-line accent">
              <span class="ledger-key">Of those, extracted</span>
              <span class="ledger-fig">{{ extractionPct }}<span class="pct-glyph">%</span></span>
            </article>
          </div>

          <!-- Progress bar — gold on parchment -->
          <div class="folio-progress">
            <div class="folio-progress-bar" :style="{ width: extractionPct + '%' }" />
          </div>
          <div class="progress-meta">
            <span><em>{{ stats.extraction_done.toLocaleString() }}</em> done</span>
            <span class="progress-sep">·</span>
            <span><em>{{ stats.extraction_pending.toLocaleString() }}</em> awaiting</span>
            <span v-if="rate !== null" class="progress-sep">·</span>
            <span v-if="rate !== null && deltaLastPoll !== 0">
              <em>{{ deltaLastPoll > 0 ? '+' : '' }}{{ deltaLastPoll }}</em> this poll
            </span>
            <span v-if="rate && rate > 0 && stats.extraction_pending > 0" class="progress-sep">·</span>
            <span v-if="rate && rate > 0 && stats.extraction_pending > 0">
              estimated finish in <em>{{ eta }}</em>
            </span>
            <span v-if="stats.extraction_pending === 0" class="progress-clear">— backlog clear</span>
          </div>

          <!-- Sparkline -->
          <div v-if="history.length > 1" class="sparkline-frame">
            <div class="sparkline">
              <div
                v-for="(h, i) in history"
                :key="i"
                class="spark-bar"
                :style="{
                  height: maxDelta > 0 ? Math.max(4, (h.delta / maxDelta) * 56) + 'px' : '4px',
                  opacity: 0.35 + (i / history.length) * 0.65,
                }"
                :title="`+${h.delta} extracted`"
              />
            </div>
            <p class="sparkline-cap">
              <em>average {{ avgRate }} per minute</em> over the last {{ history.length }} polls
            </p>
          </div>
        </section>

        <!-- II — BREAKDOWN: SOURCES, CATEGORIES, INSTRUMENTS -->
        <section v-reveal class="quire-trio">
          <article class="trio-col">
            <div class="quire-head">
              <span class="quire-numeral">II.</span>
              <span class="quire-title">By source</span>
            </div>
            <ul class="bar-list">
              <RouterLink
                v-for="src in topSources"
                :key="src.source_type"
                :to="{ path: '/threads', query: { source: src.source_type } }"
                custom
                v-slot="{ navigate }"
              >
                <li
                  class="bar-row clickable"
                  tabindex="0"
                  role="button"
                  :aria-label="`Open threads from ${src.source_type}`"
                  @click="navigate"
                  @keydown.enter.prevent="navigate"
                  @keydown.space.prevent="navigate"
                >
                  <span class="bar-label">{{ src.source_type }}</span>
                  <span class="bar-track">
                    <span class="bar-fill primary" :style="{ width: ((src.cnt / stats.total_memories) * 100) + '%' }" />
                  </span>
                  <span class="bar-num">{{ src.cnt.toLocaleString() }}</span>
                </li>
              </RouterLink>
            </ul>
          </article>

          <article class="trio-col">
            <div class="quire-head">
              <span class="quire-numeral">III.</span>
              <span class="quire-title">By category</span>
            </div>
            <ul class="bar-list">
              <RouterLink
                v-for="cat in topCategories"
                :key="cat.category"
                :to="{ path: '/memories', query: { category: cat.category } }"
                custom
                v-slot="{ navigate }"
              >
                <li
                  class="bar-row clickable"
                  tabindex="0"
                  role="button"
                  :aria-label="`Open memories in category ${cat.category}`"
                  @click="navigate"
                  @keydown.enter.prevent="navigate"
                  @keydown.space.prevent="navigate"
                >
                  <span class="bar-label">{{ cat.category }}</span>
                  <span class="bar-track">
                    <span class="bar-fill secondary" :style="{ width: ((cat.cnt / stats.total_memories) * 100) + '%' }" />
                  </span>
                  <span class="bar-num">{{ cat.cnt.toLocaleString() }}</span>
                </li>
              </RouterLink>
            </ul>
          </article>

          <article class="trio-col">
            <div class="quire-head">
              <span class="quire-numeral">IV.</span>
              <span class="quire-title">By instrument</span>
            </div>
            <ul class="bar-list">
              <RouterLink
                v-for="m in topMachines"
                :key="m.source_machine"
                :to="{ path: '/memories', query: { machine: m.source_machine } }"
                custom
                v-slot="{ navigate }"
              >
                <li
                  class="bar-row clickable"
                  tabindex="0"
                  role="button"
                  :aria-label="`Open memories from instrument ${m.source_machine}`"
                  @click="navigate"
                  @keydown.enter.prevent="navigate"
                  @keydown.space.prevent="navigate"
                >
                  <span class="bar-label">{{ m.source_machine }}</span>
                  <span class="bar-track">
                    <span class="bar-fill tertiary" :style="{ width: ((m.cnt / stats.total_memories) * 100) + '%' }" />
                  </span>
                  <span class="bar-num">{{ m.cnt.toLocaleString() }}</span>
                </li>
              </RouterLink>
            </ul>
          </article>
        </section>

        <!-- V — LATE LABOUR: LIVE LLM ACTIVITY -->
        <section v-if="health" v-reveal class="quire">
          <div class="quire-head">
            <span class="quire-numeral">V.</span>
            <span class="quire-title">Of late labour</span>
            <span class="quire-flag" :class="{ active: health.llm_activity && health.llm_activity.active_calls > 0 }">
              {{ health.llm_activity?.active_calls ?? 0 }} in flight
            </span>
          </div>

          <!-- Currently processing -->
          <div v-if="health.write_queue?.currently_processing?.length" class="labour-section">
            <p class="labour-eyebrow">Now writing</p>
            <article
              v-for="row in health.write_queue.currently_processing"
              :key="row.id"
              class="labour-row clickable"
              @click="openQueueRow(row)"
            >
              <span class="labour-cat" :class="`cat-${categoryColor(row.category)}`">
                {{ row.category || 'unmarked' }}
              </span>
              <span class="labour-text">{{ row.summary || row.content_preview }}</span>
              <span class="labour-age">{{ formatAge(row.age_s) }}</span>
            </article>
          </div>

          <!-- Pending backlog -->
          <div v-if="health.write_queue?.pending_by_category?.length" class="labour-section">
            <p class="labour-eyebrow">Awaiting the pen</p>
            <div class="pending-row">
              <span
                v-for="c in health.write_queue.pending_by_category"
                :key="c.category"
                class="pending-tag"
                :class="`cat-${categoryColor(c.category)}`"
              >
                {{ c.category || 'unmarked' }}
                <em>{{ c.count }}</em>
              </span>
            </div>
          </div>

          <!-- Recent calls -->
          <div v-if="recentCallsReversed.length" class="labour-section">
            <p class="labour-eyebrow">
              Recent inquiries
              <span class="labour-eyebrow-meta">·  avg {{ avgDuration }}″ ·  {{ liveRatio }}% live</span>
            </p>
            <TransitionGroup name="llm-list" tag="div" class="ticker">
              <article
                v-for="call in recentCallsReversed"
                :key="call.started_at"
                class="ticker-line clickable"
                :class="`ticker-${call.status}`"
                @click="openLlmCall(call)"
              >
                <span class="ticker-mark" :class="`mark-${call.caller_kind}`">
                  {{ call.caller_kind === 'live' ? '✦' : call.caller_kind === 'scheduler' ? '◇' : '·' }}
                </span>
                <span class="ticker-text">{{ call.prompt_preview || '— no prompt recorded —' }}</span>
                <span class="ticker-dur">{{ displayDuration(call, now) }}</span>
                <span class="ticker-status">
                  <span v-if="call.status === 'in_flight'" class="status-flight">⟳</span>
                  <span v-else-if="call.status === 'ok'" class="status-ok">✓</span>
                  <span v-else-if="call.status === 'timeout'" class="status-timeout">⌛</span>
                  <span v-else class="status-err">✗</span>
                </span>
              </article>
            </TransitionGroup>
          </div>

          <p v-if="!health.write_queue?.currently_processing?.length && !recentCallsReversed.length"
             class="labour-empty">— the desk is quiet —</p>
        </section>

      </template>
    </div>

    <!-- DETAIL DIALOG — folio page treatment -->
    <Teleport to="body">
      <div v-if="showDetail" class="folio-overlay" @click.self="showDetail = false">
        <div class="folio-page">
          <div class="page-header">
            <span class="ornament-sm">❦</span>
            <span class="page-kicker">
              <template v-if="detailKind === 'queue'">Write queue · entry</template>
              <template v-else>{{ detailCall?.caller_kind || 'inquiry' }} · {{ detailCall?.status }}</template>
            </span>
            <button class="page-close" aria-label="Close detail" @click="showDetail = false">×</button>
          </div>

          <div class="page-content">
            <template v-if="detailKind === 'queue' && detailQueue">
              <p class="page-eyebrow">Category</p>
              <p class="page-meta-line">
                <span class="page-tag" :class="`cat-${categoryColor(detailQueue.category)}`">
                  {{ detailQueue.category || 'unmarked' }}
                </span>
              </p>

              <template v-if="detailQueue.summary">
                <p class="page-eyebrow">Summary</p>
                <p class="page-body">{{ detailQueue.summary }}</p>
              </template>

              <p class="page-eyebrow">Content</p>
              <p v-if="loadingQueueContent" class="page-loading">
                <Dotty />
              </p>
              <p v-else class="page-pre">{{ queueFullContent || detailQueue.content_preview }}</p>

              <p class="page-source">
                <em>at the desk</em> {{ formatAge(detailQueue.age_s) }} · <em>id</em> {{ detailQueue.id.slice(0, 8) }}
              </p>
            </template>

            <template v-else-if="detailKind === 'llm' && detailCall">
              <p class="page-eyebrow">Status · duration</p>
              <p class="page-meta-line">
                <span class="page-tag">{{ detailCall.status }}</span>
                <span class="page-dur">{{ displayDuration(detailCall, now) }}</span>
              </p>
              <p class="page-eyebrow">Prompt</p>
              <p class="page-pre">{{ detailCall.prompt_preview || '— no prompt recorded —' }}</p>
            </template>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import api from '@/api/client'
import type { Stats } from '@/types'
import Dotty from '@/components/Dotty.vue'

const stats = ref<Stats | null>(null)
const pulsing = ref(false)
const countdown = ref(10)

interface HistoryPoint { ts: number; done: number; delta: number }
const history = ref<HistoryPoint[]>([])
const lastDone = ref<number | null>(null)

interface LlmCall {
  started_at: number
  caller_kind: string
  prompt_preview: string
  status: string
  duration_ms: number
}
interface HealthPayload {
  llm_activity?: { active_calls: number; recent_calls: LlmCall[] }
  write_queue?: {
    depth: number
    stale_processing: number
    currently_processing?: Array<{
      id: string; category: string | null; age_s: number;
      summary: string; content_preview: string; skip_dedup: boolean;
    }>
    pending_by_category?: Array<{ category: string | null; count: number }>
    recent_completions?: Array<{ id: string; category: string | null; result_status: string; duration_s: number; summary: string }>
  }
}
const health = ref<HealthPayload | null>(null)

interface QueueRow { id: string; category: string | null; age_s: number; summary: string; content_preview: string; skip_dedup: boolean }
const showDetail = ref(false)
const detailKind = ref<'queue' | 'llm' | ''>('')
const detailQueue = ref<QueueRow | null>(null)
const detailCall = ref<LlmCall | null>(null)
const queueFullContent = ref('')
const loadingQueueContent = ref(false)

async function openQueueRow(row: QueueRow) {
  detailKind.value = 'queue'
  detailQueue.value = row
  queueFullContent.value = ''
  showDetail.value = true
  if (row.id) {
    loadingQueueContent.value = true
    try {
      const { data } = await api.get<{ content?: string }>(`/api/memories/${row.id}`)
      queueFullContent.value = data.content ?? ''
    } catch { /* show preview fallback */ }
    finally { loadingQueueContent.value = false }
  }
}

function openLlmCall(call: LlmCall) {
  detailKind.value = 'llm'
  detailCall.value = call
  showDetail.value = true
}

const POLL_INTERVAL = 10_000

let pollTimer: ReturnType<typeof setInterval> | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null

const now = ref(Date.now() / 1000)
let tickTimer: ReturnType<typeof setInterval> | null = null

const recentCallsReversed = computed(() =>
  (health.value?.llm_activity?.recent_calls ?? []).slice().reverse().slice(0, 12)
)

const avgDuration = computed(() => {
  const done = recentCallsReversed.value.filter(c => c.status !== 'in_flight' && c.duration_ms > 0)
  if (!done.length) return '—'
  const mean = done.reduce((a, c) => a + c.duration_ms, 0) / done.length / 1000
  return mean.toFixed(1)
})

const liveRatio = computed(() => {
  const calls = recentCallsReversed.value
  if (!calls.length) return 0
  const live = calls.filter(c => c.caller_kind === 'live').length
  return Math.round((live / calls.length) * 100)
})

function formatAge(s: number): string {
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
}

function displayDuration(call: LlmCall, nowSec: number): string {
  if (call.status === 'in_flight') {
    const elapsed = nowSec - call.started_at
    if (elapsed < 1) return '<1s'
    if (elapsed < 60) return `${elapsed.toFixed(0)}s`
    return `${Math.floor(elapsed / 60)}m${Math.floor(elapsed % 60)}s`
  }
  if (!call.duration_ms) return '—'
  if (call.duration_ms < 1000) return `${call.duration_ms}ms`
  if (call.duration_ms < 60_000) return `${(call.duration_ms / 1000).toFixed(1)}s`
  return `${Math.floor(call.duration_ms / 60_000)}m${Math.floor((call.duration_ms % 60_000) / 1000)}s`
}
function categoryColor(cat: string | null | undefined): string {
  if (!cat) return 'default'
  if (cat === 'decision') return 'primary'
  if (cat === 'insight') return 'secondary'
  if (cat === 'session-log') return 'default'
  if (cat === 'architecture') return 'info'
  if (cat === 'debugging') return 'warning'
  if (cat === 'infrastructure') return 'success'
  return 'default'
}

async function fetchHealth() {
  try {
    const { data } = await api.get<HealthPayload>('/api/health/detailed')
    health.value = data
  } catch { /* transient — next tick will retry */ }
}

async function fetchStats() {
  try {
    const { data } = await api.get<Stats>('/api/stats')
    const prevDone = lastDone.value
    const delta = prevDone !== null ? data.extraction_done - prevDone : 0
    lastDone.value = data.extraction_done
    history.value = [
      ...history.value.slice(-29),
      { ts: Date.now(), done: data.extraction_done, delta: Math.max(0, delta) },
    ]
    stats.value = data
    pulsing.value = true
    setTimeout(() => { pulsing.value = false }, 600)
  } catch {
    // silent
  }
}

const extractionPct = computed(() => {
  if (!stats.value) return 0
  const total = stats.value.extraction_done + stats.value.extraction_pending
  return total > 0 ? Math.round((stats.value.extraction_done / total) * 100) : 100
})

const deltaLastPoll = computed(() => {
  if (history.value.length < 2) return 0
  return history.value[history.value.length - 1].delta
})

const rate = computed((): number | null => {
  if (history.value.length < 2) return null
  const recent = history.value.slice(-6)
  const totalDelta = recent.reduce((s, h) => s + h.delta, 0)
  const minutes = (recent.length * POLL_INTERVAL) / 60000
  return Math.round(totalDelta / minutes)
})

const avgRate = computed(() => {
  if (history.value.length < 2) return 0
  const total = history.value.reduce((s, h) => s + h.delta, 0)
  const minutes = (history.value.length * POLL_INTERVAL) / 60000
  return Math.round(total / minutes)
})

const eta = computed(() => {
  if (!stats.value || !rate.value || rate.value <= 0) return '—'
  const minsLeft = Math.ceil(stats.value.extraction_pending / rate.value)
  if (minsLeft < 60) return `~${minsLeft}m`
  const h = Math.floor(minsLeft / 60)
  const m = minsLeft % 60
  return `~${h}h ${m}m`
})

const maxDelta = computed(() => Math.max(...history.value.map(h => h.delta), 1))

const topSources = computed(() => {
  if (!stats.value) return []
  return [...stats.value.by_source].sort((a, b) => b.cnt - a.cnt).slice(0, 8)
})

const topCategories = computed(() => {
  if (!stats.value) return []
  return stats.value.by_category
    .filter(c => !c.category.startsWith('_') && c.cnt > 5)
    .sort((a, b) => b.cnt - a.cnt)
    .slice(0, 8)
})

const topMachines = computed(() => {
  if (!stats.value) return []
  return [...stats.value.by_machine].sort((a, b) => b.cnt - a.cnt).slice(0, 8)
})

onMounted(() => {
  fetchStats()
  fetchHealth()
  pollTimer = setInterval(() => { fetchStats(); fetchHealth() }, POLL_INTERVAL)
  tickTimer = setInterval(() => { now.value = Date.now() / 1000 }, 1000)
  countdownTimer = setInterval(() => {
    countdown.value = countdown.value <= 1 ? 10 : countdown.value - 1
  }, 1000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (countdownTimer) clearInterval(countdownTimer)
  if (tickTimer) clearInterval(tickTimer)
})
</script>

<style scoped>
.pulse-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  --cp-bg-deep: rgba(14, 11, 6, 0.55);
  --cat-primary: #c8a96e;
  --cat-secondary: #9d6c4a;
  --cat-info: #6e8fa9;
  --cat-warning: #c89e6e;
  --cat-success: #8aa96e;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 32px 24px 80px;
  min-height: 100vh;
}

.pulse-shell {
  max-width: 980px;
  margin: 0 auto;
}

/* MASTHEAD */
.pulse-masthead { margin-bottom: 36px; }
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
  text-transform: uppercase;
  color: var(--cp-gold);
  font-size: 10px;
}
.live-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-style: italic;
  letter-spacing: 0.05em;
}
.live-pulse {
  display: inline-block;
  color: var(--cp-gold-soft);
  font-size: 14px;
  transition: color 600ms ease, transform 600ms cubic-bezier(0.22, 1, 0.36, 1);
}
.live-pulse.active {
  color: var(--cp-gold);
  transform: scale(1.25);
}
.live-cd {
  color: var(--cp-ink-mute);
  font-variant-numeric: tabular-nums;
}
.pulse-title {
  font-family: Georgia, serif;
  font-size: clamp(34px, 4.5vw, 48px);
  font-weight: 400;
  letter-spacing: 0.02em;
  margin: 0 0 4px;
  color: var(--cp-ink);
}
.pulse-tagline {
  font-style: italic;
  color: var(--cp-ink-mute);
  font-size: 14px;
  margin: 0 0 16px;
}

/* QUIRE — section block */
.quire {
  margin-bottom: 56px;
  padding: 0;
}
.quire-head {
  display: grid;
  grid-template-columns: 50px 1fr auto;
  gap: 12px;
  align-items: baseline;
  padding-bottom: 10px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.quire-numeral {
  font-family: Georgia, serif;
  font-size: 28px;
  color: var(--cp-gold);
  font-weight: 300;
  text-align: right;
  font-style: italic;
}
.quire-title {
  font-family: Georgia, serif;
  font-size: 18px;
  font-style: italic;
  color: var(--cp-ink);
  letter-spacing: 0.03em;
}
.quire-flag {
  font-family: Georgia, serif;
  font-style: italic;
  color: var(--cp-ink-mute);
  font-size: 13px;
}
.quire-flag.active {
  color: var(--cp-gold);
}

/* LEDGER FIGURES */
.ledger-figures {
  display: grid;
  gap: 0;
  margin-bottom: 24px;
}
.ledger-line {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: baseline;
  padding: 10px 8px 10px 64px;
  border-bottom: 1px dotted var(--cp-gold-faint);
  font-family: Georgia, serif;
}
.ledger-line.accent {
  background: linear-gradient(90deg, transparent, rgba(200, 169, 110, 0.06));
  border-bottom-color: var(--cp-gold-soft);
  border-bottom-style: solid;
}
.ledger-key {
  font-style: italic;
  font-size: 14px;
  color: var(--cp-ink-mute);
}
.ledger-fig {
  font-size: 22px;
  color: var(--cp-ink);
  font-variant-numeric: tabular-nums;
  font-weight: 400;
}
.ledger-line.accent .ledger-fig {
  color: var(--cp-gold);
}
.pct-glyph {
  font-size: 14px;
  margin-left: 1px;
  color: var(--cp-ink-mute);
}

/* PROGRESS BAR */
.folio-progress {
  height: 6px;
  background: rgba(200, 169, 110, 0.08);
  border-top: 1px solid var(--cp-gold-faint);
  border-bottom: 1px solid var(--cp-gold-faint);
  margin: 16px 64px 8px;
  position: relative;
  overflow: hidden;
}
.folio-progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--cp-gold-soft), var(--cp-gold));
  transition: width 600ms cubic-bezier(0.22, 1, 0.36, 1);
}
.progress-meta {
  font-family: Georgia, serif;
  font-size: 13px;
  font-style: italic;
  color: var(--cp-ink-mute);
  text-align: left;
  margin: 0 64px 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
}
.progress-meta em { color: var(--cp-ink); font-style: normal; font-variant-numeric: tabular-nums; }
.progress-sep { color: var(--cp-gold-soft); }
.progress-clear { color: var(--cp-gold); font-style: italic; }

/* SPARKLINE */
.sparkline-frame {
  margin: 16px 64px 0;
  padding: 16px 0 0;
  border-top: 1px dotted var(--cp-gold-faint);
}
.sparkline {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 60px;
}
.spark-bar {
  flex: 1;
  background: var(--cp-gold);
  min-height: 4px;
  transition: height 300ms cubic-bezier(0.22, 1, 0.36, 1);
}
.sparkline-cap {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink-mute);
  margin: 8px 0 0;
}
.sparkline-cap em { color: var(--cp-ink); font-style: normal; }

/* QUIRE TRIO — three-column layout */
.quire-trio {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 36px;
  margin-bottom: 56px;
}
@media (max-width: 760px) {
  .quire-trio { grid-template-columns: 1fr; gap: 36px; }
}
.trio-col .quire-head {
  grid-template-columns: 32px 1fr;
}
.trio-col .quire-numeral { font-size: 22px; }
.trio-col .quire-title { font-size: 14px; }

/* BAR LIST */
.bar-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.bar-list li {
  display: grid;
  grid-template-columns: 90px 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px dotted var(--cp-gold-faint);
}
.bar-list li:last-child { border-bottom: none; }
.bar-list li.clickable {
  cursor: pointer;
  border-radius: 2px;
  margin: 0 -6px;
  padding: 6px 6px;
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    transform var(--cp-dur-hover) var(--cp-ease);
}
.bar-list li.clickable:hover,
.bar-list li.clickable:focus-visible {
  background: var(--cp-gold-trace);
  transform: translateX(2px);
}
.bar-list li.clickable:focus-visible { outline: none; }
.bar-label {
  font-family: Georgia, serif;
  font-size: 12px;
  font-style: italic;
  color: var(--cp-ink-mute);
  letter-spacing: 0.02em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar-track {
  height: 4px;
  background: rgba(200, 169, 110, 0.08);
  position: relative;
  display: block;
}
.bar-fill {
  display: block;
  height: 100%;
  transition: width 500ms cubic-bezier(0.22, 1, 0.36, 1);
}
.bar-fill.primary { background: var(--cp-gold); }
.bar-fill.secondary { background: var(--cat-secondary); }
.bar-fill.tertiary { background: var(--cat-info); }
.bar-num {
  font-family: Georgia, serif;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--cp-ink);
  min-width: 56px;
  text-align: right;
}

/* LATE LABOUR */
.labour-section {
  margin-bottom: 24px;
  padding-left: 64px;
}
.labour-eyebrow {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold);
  margin: 0 0 8px;
}
.labour-eyebrow-meta {
  color: var(--cp-ink-mute);
  text-transform: none;
  letter-spacing: 0.05em;
  font-size: 11px;
}

.labour-row {
  display: grid;
  grid-template-columns: 100px 1fr auto;
  gap: 12px;
  align-items: baseline;
  padding: 8px 0;
  border-bottom: 1px dotted var(--cp-gold-faint);
  cursor: pointer;
  transition: padding 150ms;
}
.labour-row:hover { padding-left: 4px; background: rgba(200, 169, 110, 0.03); }
.labour-cat {
  font-size: 10px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  font-style: italic;
  color: var(--cp-ink-mute);
}
.labour-text {
  font-family: Georgia, serif;
  font-size: 13px;
  color: var(--cp-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.labour-age {
  font-family: Georgia, serif;
  font-style: italic;
  font-variant-numeric: tabular-nums;
  font-size: 11px;
  color: var(--cp-ink-mute);
}

/* PENDING TAGS */
.pending-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  align-items: baseline;
}
.pending-tag {
  font-family: Georgia, serif;
  font-size: 12px;
  font-style: italic;
  color: var(--cp-ink-mute);
}
.pending-tag em {
  color: var(--cp-ink);
  font-style: normal;
  font-variant-numeric: tabular-nums;
  margin-left: 4px;
  padding: 0 4px;
  background: rgba(200, 169, 110, 0.1);
}

/* TICKER */
.ticker { display: flex; flex-direction: column; }
.ticker-line {
  display: grid;
  grid-template-columns: 18px 1fr 56px 18px;
  gap: 8px;
  align-items: baseline;
  padding: 7px 0;
  border-bottom: 1px dotted var(--cp-gold-faint);
  cursor: pointer;
  transition: all 150ms;
}
.ticker-line:hover { padding-left: 4px; background: rgba(200, 169, 110, 0.03); }
.ticker-mark {
  text-align: center;
  font-size: 11px;
  color: var(--cp-gold-soft);
}
.ticker-mark.mark-live { color: var(--cp-gold); }
.ticker-mark.mark-scheduler { color: var(--cat-info); }
.ticker-text {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ticker-dur {
  font-family: Georgia, serif;
  font-variant-numeric: tabular-nums;
  font-size: 11px;
  color: var(--cp-ink-mute);
  text-align: right;
}
.ticker-status { text-align: center; font-size: 12px; }
.status-flight { color: var(--cat-info); display: inline-block; animation: spin-slow 1.4s linear infinite; }
.status-ok { color: var(--cat-success); }
.status-timeout { color: var(--cat-warning); }
.status-err { color: #c47a6a; }
@keyframes spin-slow { to { transform: rotate(360deg); } }

.ticker-in_flight { background: linear-gradient(90deg, transparent, rgba(110, 143, 169, 0.04)); }
.ticker-timeout { color: var(--cat-warning); }
.ticker-error { background: rgba(196, 122, 106, 0.04); }

/* TRANSITION */
.llm-list-enter-active, .llm-list-leave-active { transition: all 350ms cubic-bezier(0.22, 1, 0.36, 1); }
.llm-list-enter-from { opacity: 0; transform: translateY(-4px); }
.llm-list-leave-to { opacity: 0; transform: translateX(8px); }

/* LOADING & EMPTY */
.loading-folio, .labour-empty {
  text-align: center;
  padding: 48px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
}
.loading-text { font-size: 13px; margin: 8px 0 0; letter-spacing: 0.05em; }
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }

/* FOLIO PAGE DIALOG */
.folio-overlay {
  position: fixed;
  inset: 0;
  background: rgba(8, 6, 3, 0.85);
  backdrop-filter: blur(4px);
  z-index: 9999;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 40px 20px;
  overflow-y: auto;
}
.folio-page {
  max-width: 720px;
  width: 100%;
  background: linear-gradient(180deg, rgba(28, 22, 13, 0.98), rgba(18, 14, 8, 0.98));
  border: 1px solid var(--cp-gold-faint);
  border-top: 3px solid var(--cp-gold);
  font-family: Georgia, serif;
  animation: page-rise 280ms cubic-bezier(0.22, 1, 0.36, 1);
}
@keyframes page-rise {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}
.page-header {
  display: flex; align-items: center; gap: 12px;
  padding: 18px 28px;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.ornament-sm { color: var(--cp-gold); font-size: 14px; }
.page-kicker {
  font-size: 10px; letter-spacing: 0.25em;
  text-transform: uppercase; color: var(--cp-gold);
  font-style: italic; flex-grow: 1;
}
.page-close {
  background: transparent; border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink-mute); font-size: 22px;
  width: 30px; height: 30px; cursor: pointer;
  font-family: Georgia, serif; line-height: 1;
  transition: all 150ms;
}
.page-close:hover { color: var(--cp-gold); border-color: var(--cp-gold); }
.page-content { padding: 24px 36px 36px; }
.page-eyebrow {
  font-family: Georgia, serif; font-style: italic;
  font-size: 11px; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--cp-gold);
  margin: 16px 0 8px;
}
.page-eyebrow:first-child { margin-top: 0; }
.page-meta-line { display: flex; gap: 8px; align-items: center; margin: 0 0 12px; }
.page-tag {
  font-size: 11px; letter-spacing: 0.05em;
  padding: 4px 10px; background: rgba(200, 169, 110, 0.15);
  color: var(--cp-ink); font-style: italic;
}
.page-dur {
  font-family: Georgia, serif; font-variant-numeric: tabular-nums;
  font-size: 13px; color: var(--cp-ink-mute);
}
.page-body {
  font-size: 16px; line-height: 1.75;
  color: var(--cp-ink); margin: 0 0 16px;
  font-family: Georgia, serif;
}
.page-pre {
  font-family: Georgia, serif; font-size: 13px;
  line-height: 1.7; color: var(--cp-ink);
  white-space: pre-wrap; margin: 0 0 16px;
  padding: 14px 16px; background: rgba(200, 169, 110, 0.04);
  border-left: 2px solid var(--cp-gold-soft);
}
.page-source {
  font-style: italic; color: var(--cp-ink-mute);
  font-size: 12px; margin: 16px 0 0;
}
.page-source em { color: var(--cp-ink-mute); }
.page-loading { text-align: center; padding: 24px 0; }

/* CATEGORY COLORS */
.cat-primary { color: var(--cp-gold); }
.cat-secondary { color: var(--cat-secondary); }
.cat-info { color: var(--cat-info); }
.cat-warning { color: var(--cat-warning); }
.cat-success { color: var(--cat-success); }
.cat-default { color: var(--cp-ink-mute); }

.clickable { cursor: pointer; }

/* MOBILE: shrink fixed-padding ledger blocks, stack multi-column grids */
@media (max-width: 720px) {
  .pulse-page { padding: 24px 14px 64px; }
  .pulse-title { font-size: 30px; }

  .quire { margin-bottom: 40px; }
  .quire-head {
    grid-template-columns: 36px 1fr auto;
    gap: 8px;
  }
  .quire-numeral { font-size: 22px; }
  .quire-title { font-size: 16px; }

  .ledger-line { padding: 10px 4px 10px 24px; }
  .ledger-fig { font-size: 18px; }
  .progress-block, .sparkline-frame { margin: 12px 0 18px; }
  .progress-meta { margin: 0 0 16px; }
  .labour-section { padding-left: 0; }

  .quire-trio { gap: 24px; }

  /* Bar lists: shrink the label column */
  .bar-list li {
    grid-template-columns: 80px 1fr auto;
    gap: 8px;
  }

  /* Labour rows: stack the category onto its own row above the prose */
  .labour-row {
    grid-template-columns: 1fr auto;
    grid-template-areas:
      "cat   age"
      "text  text";
    row-gap: 4px;
    column-gap: 8px;
  }
  .labour-cat { grid-area: cat; }
  .labour-age { grid-area: age; }
  .labour-text {
    grid-area: text;
    white-space: normal;
    word-break: break-word;
  }

  /* Ticker rows: narrower duration column, wrap prose */
  .ticker-line {
    grid-template-columns: 14px 1fr 44px 14px;
    gap: 6px;
  }
  .ticker-text {
    white-space: normal;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
}
</style>
