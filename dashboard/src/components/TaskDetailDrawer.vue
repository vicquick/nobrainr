<template>
  <Transition name="cp-drawer">
    <aside
      v-if="taskName"
      ref="rootEl"
      class="task-drawer"
      role="dialog"
      :aria-labelledby="`drawer-title-${taskName}`"
      tabindex="-1"
      @click.self="$emit('close')"
    >
      <!-- Backdrop -->
      <div class="drawer-backdrop" @click="$emit('close')" />

      <!-- Panel -->
      <div class="drawer-panel" @keydown.esc.stop="$emit('close')">
        <header class="drawer-head">
          <div class="drawer-head-row">
            <span class="drawer-eyebrow">Discipline</span>
            <button class="drawer-close" aria-label="close" @click="$emit('close')">×</button>
          </div>
          <h2 :id="`drawer-title-${taskName}`" class="drawer-title">
            {{ taskName }}
          </h2>
          <p class="drawer-tagline">
            <em>recent observances of this office</em>
          </p>
          <div class="masthead-rule" />
        </header>

        <div v-if="loading" class="drawer-loading">
          <Dotty />
          <span class="loading-text">consulting the chronicle</span>
        </div>

        <template v-else-if="data">
          <!-- Tally row -->
          <div class="drawer-tally">
            <article class="tally-cell">
              <span class="tally-fig accent-good">{{ data.tally.ok ?? 0 }}</span>
              <span class="tally-key">kept</span>
            </article>
            <article class="tally-cell">
              <span class="tally-fig accent-bad">{{ data.tally.failed ?? 0 }}</span>
              <span class="tally-key">failed</span>
            </article>
            <article class="tally-cell">
              <span class="tally-fig accent-warn">{{ data.tally.timeout ?? 0 }}</span>
              <span class="tally-key">timeout</span>
            </article>
            <article v-if="data.tally.running" class="tally-cell">
              <span class="tally-fig">{{ data.tally.running }}</span>
              <span class="tally-key">in flight</span>
            </article>
          </div>

          <!-- Sparkline -->
          <section v-if="durations.length" class="drawer-section">
            <div class="section-head">
              <span class="section-numeral">I.</span>
              <span class="section-title">Sparkline · duration</span>
              <span class="section-flag">{{ durations.length }} runs</span>
            </div>
            <svg
              :viewBox="`0 0 ${SPARK_W} ${SPARK_H}`"
              class="sparkline"
              preserveAspectRatio="none"
              role="img"
              aria-label="recent run durations"
            >
              <!-- Mean reference line -->
              <line
                v-if="meanY !== null"
                :x1="0" :x2="SPARK_W" :y1="meanY" :y2="meanY"
                class="spark-mean"
              />
              <!-- Path -->
              <path :d="sparkPath" class="spark-line" />
              <!-- Points -->
              <circle
                v-for="(p, i) in sparkPoints"
                :key="i"
                :cx="p.x"
                :cy="p.y"
                :r="i === sparkPoints.length - 1 ? 2.4 : 1.4"
                :class="['spark-point', `spark-${p.status}`]"
              />
            </svg>
            <div class="spark-legend">
              <span><strong>{{ formatDur(maxDur) }}</strong> peak</span>
              <span class="dot">·</span>
              <span><strong>{{ formatDur(meanDur) }}</strong> mean</span>
              <span class="dot">·</span>
              <span><strong>{{ formatDur(minDur) }}</strong> trough</span>
            </div>
          </section>

          <!-- Last error -->
          <section v-if="data.last_error" class="drawer-section">
            <div class="section-head">
              <span class="section-numeral">II.</span>
              <span class="section-title">Last error</span>
              <span class="section-flag">{{ formatRelative(data.last_error.started_at) }}</span>
            </div>
            <pre class="drawer-error">{{ data.last_error.error_msg }}</pre>
          </section>

          <!-- Recent runs list -->
          <section class="drawer-section">
            <div class="section-head">
              <span class="section-numeral">{{ data.last_error ? 'III.' : 'II.' }}</span>
              <span class="section-title">Annotationes · recent runs</span>
            </div>
            <ul class="run-list">
              <template v-for="run in data.runs.slice(0, 20)" :key="run.id">
                <li
                  class="run-line"
                  :class="[`run-${run.status}`, { 'run-line-open': openRunId === run.id }]"
                  tabindex="0"
                  role="button"
                  :aria-expanded="openRunId === run.id"
                  :aria-label="`Run #${run.id}, ${run.status}, started ${formatRelative(run.started_at)}`"
                  @click="toggleRun(run.id)"
                  @keydown.enter.prevent="toggleRun(run.id)"
                  @keydown.space.prevent="toggleRun(run.id)"
                >
                  <span class="run-status">{{ statusGlyph(run.status) }}</span>
                  <span class="run-time">{{ formatRelative(run.started_at) }}</span>
                  <span class="run-dur">{{ run.duration_ms !== null ? formatDur(run.duration_ms) : '—' }}</span>
                </li>
                <!-- Expanded detail row — grid-rows 0fr → 1fr animates
                     the height without measuring; works because the
                     inner content stays absolutely-sized via the grid
                     track collapse. CSS-only, GPU-friendly. -->
                <li
                  v-if="openRunId === run.id"
                  class="run-detail"
                  :class="`run-${run.status}`"
                >
                  <div class="run-detail-grid">
                    <div class="run-detail-row">
                      <span class="run-detail-key">id</span>
                      <span class="run-detail-val">#{{ run.id }}</span>
                    </div>
                    <div class="run-detail-row">
                      <span class="run-detail-key">started</span>
                      <span class="run-detail-val">{{ formatAbsolute(run.started_at) }}</span>
                    </div>
                    <div v-if="run.finished_at" class="run-detail-row">
                      <span class="run-detail-key">finished</span>
                      <span class="run-detail-val">{{ formatAbsolute(run.finished_at) }}</span>
                    </div>
                    <div class="run-detail-row">
                      <span class="run-detail-key">duration</span>
                      <span class="run-detail-val">
                        {{ run.duration_ms !== null ? formatDur(run.duration_ms) : 'in flight' }}
                      </span>
                    </div>
                    <div class="run-detail-row">
                      <span class="run-detail-key">status</span>
                      <span class="run-detail-val" :class="`run-${run.status}`">{{ run.status }}</span>
                    </div>
                  </div>
                  <pre v-if="run.error_msg" class="run-detail-error">{{ run.error_msg }}</pre>
                </li>
              </template>
            </ul>
          </section>
        </template>

        <p v-else-if="!loading" class="drawer-empty">— no observances yet —</p>
      </div>
    </aside>
  </Transition>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import api from '@/api/client'
import Dotty from './Dotty.vue'

interface Run {
  id: number
  started_at: string
  finished_at: string | null
  status: 'ok' | 'failed' | 'timeout' | 'running'
  duration_ms: number | null
  error_msg: string | null
}
interface DrawerData {
  task_name: string
  runs: Run[]
  tally: Record<string, number>
  last_error: Run | null
}

const props = defineProps<{ taskName: string | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const loading = ref(false)
const data = ref<DrawerData | null>(null)
const rootEl = ref<HTMLElement | null>(null)

const SPARK_W = 320
const SPARK_H = 60

watch(
  () => props.taskName,
  async (name) => {
    if (!name) { data.value = null; return }
    loading.value = true
    data.value = null
    try {
      const res = await api.get(`/api/scheduler/task/${encodeURIComponent(name)}`)
      data.value = res.data
    } finally {
      loading.value = false
      await nextTick()
      rootEl.value?.focus()
    }
  },
  { immediate: true },
)

// Keyboard handling: ESC closes; Tab is constrained to focusable
// descendants of the drawer so screen-reader / keyboard users can't
// accidentally tab back into the (visually backgrounded) page below.
// Standard focus-trap pattern: on Tab, find the first/last focusable,
// loop the cursor.
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusables(): HTMLElement[] {
  if (!rootEl.value) return []
  return Array.from(
    rootEl.value.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter((el) => !el.hasAttribute('disabled') && el.offsetParent !== null)
}

function onKey(e: KeyboardEvent) {
  if (!props.taskName) return
  if (e.key === 'Escape') {
    emit('close')
    return
  }
  if (e.key !== 'Tab') return
  const list = focusables()
  if (list.length === 0) return
  const first = list[0]
  const last = list[list.length - 1]
  const active = document.activeElement as HTMLElement | null
  // Cursor outside the drawer (e.g. user just opened it and focus was
  // never moved into a focusable child) — pull it to first/last.
  if (!active || !rootEl.value?.contains(active)) {
    e.preventDefault()
    ;(e.shiftKey ? last : first).focus()
    return
  }
  if (e.shiftKey && active === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))

const durations = computed(() =>
  (data.value?.runs ?? [])
    .filter((r) => r.duration_ms !== null && r.status !== 'running')
    .slice(0, 30)
    .reverse(), // chronological for the sparkline
)
const maxDur = computed(() => Math.max(0, ...durations.value.map((r) => r.duration_ms ?? 0)))
const minDur = computed(() => durations.value.length
  ? Math.min(...durations.value.map((r) => r.duration_ms ?? 0))
  : 0,
)
const meanDur = computed(() => {
  const xs = durations.value.map((r) => r.duration_ms ?? 0)
  return xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : 0
})

const sparkPoints = computed(() => {
  const xs = durations.value
  if (!xs.length) return []
  const max = maxDur.value || 1
  const stepX = xs.length > 1 ? SPARK_W / (xs.length - 1) : SPARK_W / 2
  return xs.map((r, i) => ({
    x: xs.length > 1 ? i * stepX : SPARK_W / 2,
    y: SPARK_H - 6 - ((r.duration_ms ?? 0) / max) * (SPARK_H - 12),
    status: r.status,
  }))
})
const sparkPath = computed(() => {
  if (!sparkPoints.value.length) return ''
  return sparkPoints.value
    .map((p, i) => (i === 0 ? `M ${p.x} ${p.y}` : `L ${p.x} ${p.y}`))
    .join(' ')
})
const meanY = computed(() => {
  const max = maxDur.value
  if (!max) return null
  return SPARK_H - 6 - (meanDur.value / max) * (SPARK_H - 12)
})

function formatDur(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms / 1000 / 60)}m`
}
function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

function formatAbsolute(iso: string): string {
  const d = new Date(iso)
  // ISO-ish but human-friendly: "2026-05-11 14:32:09 UTC"
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC'
}

const openRunId = ref<number | null>(null)
function toggleRun(id: number) {
  openRunId.value = openRunId.value === id ? null : id
}
function statusGlyph(s: string): string {
  return s === 'ok' ? '✓' : s === 'failed' ? '✗' : s === 'timeout' ? '⌛' : '∘'
}
</script>

<style scoped>
.task-drawer {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  justify-content: flex-end;
}
.drawer-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(8, 6, 2, 0.45);
  backdrop-filter: blur(2px);
}
.drawer-panel {
  position: relative;
  width: min(440px, 100vw);
  height: 100vh;
  overflow-y: auto;
  background:
    radial-gradient(800px 600px at 80% 0%, rgba(200, 169, 110, 0.04), transparent 60%),
    linear-gradient(180deg, var(--cp-paper) 0%, var(--cp-paper-deep) 100%);
  border-left: 1px solid var(--cp-rule);
  box-shadow: -8px 0 32px rgba(0, 0, 0, 0.5);
  padding: 28px 28px 48px;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
}

.drawer-head { margin-bottom: 24px; }
.drawer-head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.drawer-eyebrow {
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
}
.drawer-close {
  background: transparent;
  border: 1px solid var(--cp-rule);
  color: var(--cp-ink-mute);
  width: 28px; height: 28px;
  border-radius: 2px;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: all var(--cp-dur-hover) var(--cp-ease);
}
.drawer-close:hover { color: var(--cp-ink); border-color: var(--cp-gold-soft); }
.drawer-title {
  font-size: 24px;
  font-weight: 400;
  margin: 8px 0 6px;
  letter-spacing: -0.005em;
  font-variant: small-caps;
}
.drawer-tagline {
  font-style: italic;
  color: var(--cp-ink-mute);
  margin: 0 0 12px;
  font-size: 13px;
}
.masthead-rule {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--cp-gold-soft), transparent);
  margin-top: 12px;
}

.drawer-loading, .drawer-empty {
  text-align: center;
  margin: 60px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
}
.loading-text { font-size: 13px; letter-spacing: 0.05em; margin-left: 12px; }

.drawer-tally {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin: 0 0 24px;
}
.drawer-tally:has(.tally-cell:nth-child(4)) { grid-template-columns: repeat(4, 1fr); }
.tally-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  border: 1px solid var(--cp-rule);
  border-radius: 2px;
  padding: 10px 4px;
}
.tally-fig {
  font-size: 24px;
  font-variant-numeric: tabular-nums;
  color: var(--cp-ink);
}
.tally-fig.accent-good { color: #6c9a6c; }
.tally-fig.accent-bad  { color: #c46a6a; }
.tally-fig.accent-warn { color: #c4a46a; }
.tally-key {
  margin-top: 2px;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
  letter-spacing: 0.05em;
}

.drawer-section { margin-bottom: 24px; }
.section-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}
.section-numeral {
  font-style: italic;
  color: var(--cp-gold-soft);
  font-size: 14px;
  min-width: 24px;
}
.section-title {
  font-style: italic;
  font-size: 13px;
  color: var(--cp-ink);
}
.section-flag {
  margin-left: auto;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
}

.sparkline {
  width: 100%;
  height: 60px;
  display: block;
}
.spark-line {
  fill: none;
  stroke: var(--cp-gold);
  stroke-width: 1.2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.spark-mean {
  stroke: var(--cp-gold-faint);
  stroke-dasharray: 2 3;
  stroke-width: 0.8;
}
.spark-point { fill: var(--cp-gold); }
.spark-failed { fill: #c46a6a; }
.spark-timeout { fill: #c4a46a; }
.spark-legend {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--cp-ink-mute);
  font-style: italic;
  margin-top: 6px;
}
.spark-legend strong {
  color: var(--cp-ink);
  font-weight: 400;
  font-variant-numeric: tabular-nums;
  font-style: normal;
}
.dot { color: var(--cp-gold-soft); }

.drawer-error {
  font-family: 'SF Mono', ui-monospace, Menlo, Consolas, monospace;
  font-size: 11.5px;
  background: rgba(196, 106, 106, 0.06);
  border-left: 2px solid #c46a6a;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
  color: rgba(238, 224, 196, 0.85);
  border-radius: 2px;
  max-height: 200px;
  overflow-y: auto;
}

.run-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.run-line {
  display: grid;
  grid-template-columns: 24px 1fr auto;
  gap: 8px;
  align-items: baseline;
  padding: 6px 8px;
  margin: 0 -8px;
  border-radius: 2px;
  border-bottom: 1px dotted rgba(200, 169, 110, 0.10);
  font-size: 13px;
  cursor: pointer;
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    transform var(--cp-dur-hover) var(--cp-ease);
}
.run-line:hover,
.run-line:focus-visible {
  background: var(--cp-gold-trace);
  transform: translateX(2px);
}
.run-line:focus-visible { outline: none; }
.run-line-open {
  background: var(--cp-gold-trace);
  border-bottom-color: transparent;
}
.run-detail {
  display: block;
  margin: 0 -8px 6px;
  padding: 10px 12px 12px;
  background: rgba(8, 6, 2, 0.35);
  border-left: 2px solid var(--cp-gold-soft);
  border-bottom: 1px dotted var(--cp-gold-faint);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  animation: run-detail-in 220ms var(--cp-ease-decel) both;
}
.run-detail.run-failed { border-left-color: #c46a6a; }
.run-detail.run-timeout { border-left-color: #c4a46a; }
@keyframes run-detail-in {
  from { opacity: 0; transform: translateY(-2px); }
  to   { opacity: 1; transform: translateY(0); }
}
.run-detail-grid {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 4px 12px;
  font-size: 12px;
}
.run-detail-row { display: contents; }
.run-detail-key {
  font-style: italic;
  letter-spacing: 0.04em;
  color: var(--cp-ink-mute);
}
.run-detail-val {
  color: var(--cp-ink);
  font-variant-numeric: tabular-nums;
}
.run-detail-val.run-failed { color: #c46a6a; }
.run-detail-val.run-timeout { color: #c4a46a; }
.run-detail-val.run-running { color: var(--cp-gold); }
.run-detail-val.run-ok { color: var(--cp-ink); }
.run-detail-error {
  font-family: 'SF Mono', ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  background: rgba(196, 106, 106, 0.06);
  border-left: 2px solid #c46a6a;
  padding: 8px 10px;
  margin: 8px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: rgba(238, 224, 196, 0.85);
  border-radius: 2px;
  max-height: 180px;
  overflow-y: auto;
}
.run-status { color: var(--cp-gold-soft); }
.run-failed .run-status { color: #c46a6a; }
.run-timeout .run-status { color: #c4a46a; }
.run-time { font-style: italic; color: var(--cp-ink-mute); }
.run-dur { font-variant-numeric: tabular-nums; color: var(--cp-ink); }

/* Slide+fade enter/leave. CSS-only transition group; motion-v
   reserved for the more elaborate drawer behaviors in later batches. */
.cp-drawer-enter-active .drawer-panel {
  transition:
    transform var(--cp-dur-panel) var(--cp-ease-decel),
    opacity 80ms linear;
}
.cp-drawer-leave-active .drawer-panel {
  transition:
    transform 200ms var(--cp-ease-accel),
    opacity var(--cp-dur-out) linear;
}
.cp-drawer-enter-from .drawer-panel,
.cp-drawer-leave-to .drawer-panel {
  transform: translateX(40px);
  opacity: 0;
}
.cp-drawer-enter-active .drawer-backdrop,
.cp-drawer-leave-active .drawer-backdrop {
  transition: opacity var(--cp-dur-panel) var(--cp-ease);
}
.cp-drawer-enter-from .drawer-backdrop,
.cp-drawer-leave-to .drawer-backdrop {
  opacity: 0;
}
</style>
