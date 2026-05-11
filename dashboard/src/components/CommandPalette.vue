<template>
  <Teleport to="body">
    <AnimatePresence>
      <Motion
        v-if="open"
        :initial="{ opacity: 0 }"
        :animate="{ opacity: 1 }"
        :exit="{ opacity: 0 }"
        :transition="{ duration: 0.18, ease: [0, 0, 0.2, 1] }"
        class="cp-shell"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cp-title"
        @click.self="close"
      >
        <div class="cp-backdrop" />
        <Motion
          :initial="{ opacity: 0, scale: 0.97, y: -8 }"
          :animate="{ opacity: 1, scale: 1, y: 0 }"
          :exit="{ opacity: 0, scale: 0.98, y: -4 }"
          :transition="{ duration: 0.20, ease: [0, 0, 0.2, 1] }"
          class="cp-card"
          @click.stop
        >
          <header class="cp-head">
            <span class="cp-eyebrow">Codex · jump</span>
            <h2 id="cp-title" class="cp-title">Search the corpus</h2>
            <p class="cp-tagline">
              <em>Routes, memories, and threads — speak a word to seek.</em>
            </p>
          </header>

          <div class="cp-input-row">
            <span class="cp-input-glyph" aria-hidden="true">❦</span>
            <input
              ref="inputEl"
              v-model="query"
              type="text"
              class="cp-input"
              placeholder="seek a route, a memory, an entity…"
              autocomplete="off"
              spellcheck="false"
              @keydown.down.prevent="moveCursor(1)"
              @keydown.up.prevent="moveCursor(-1)"
              @keydown.enter.prevent="activateCursor"
              @keydown.esc.prevent="close"
            />
            <kbd class="cp-input-kbd">Esc</kbd>
          </div>

          <div ref="listEl" class="cp-list">
            <!-- Recent queries — only when input is empty AND there's
                 history to surface. Lets the reader pull a previous
                 search back into the input with one keystroke. -->
            <template v-if="showRecent">
              <p class="cp-section">
                Recent
                <span class="cp-section-count">· {{ recentQueries.length }}</span>
              </p>
              <button
                v-for="(q, k) in recentQueries"
                :key="`recent-${q}`"
                class="cp-row cp-row-recent"
                :class="{ 'cp-row-active': cursor === k }"
                role="option"
                :aria-selected="cursor === k"
                @mouseenter="cursor = k"
                @click="recallQuery(q)"
              >
                <span class="cp-row-glyph" aria-hidden="true">↻</span>
                <span class="cp-row-text">{{ q }}</span>
                <span
                  class="cp-row-recent-dismiss"
                  role="button"
                  tabindex="-1"
                  aria-label="Forget this query"
                  title="Forget"
                  @click.stop="forgetQuery(q)"
                >×</span>
              </button>
            </template>

            <!-- Routes section -->
            <template v-if="filteredRoutes.length">
              <p class="cp-section">Routes</p>
              <button
                v-for="(r, i) in filteredRoutes"
                :key="r.to"
                ref="routeRowsEl"
                class="cp-row cp-row-route"
                :class="{ 'cp-row-active': cursor === routeBase + i }"
                role="option"
                :aria-selected="cursor === routeBase + i"
                @mouseenter="cursor = routeBase + i"
                @click="goRoute(r.to)"
              >
                <span class="cp-row-glyph" aria-hidden="true">→</span>
                <span class="cp-row-label">{{ r.label }}</span>
                <span class="cp-row-path">{{ r.to }}</span>
              </button>
            </template>

            <!-- Memories section -->
            <template v-if="memoryHits.length">
              <p class="cp-section">
                Memories
                <span class="cp-section-count">· {{ memoryHits.length }}</span>
                <span v-if="memoriesLoading" class="cp-section-loading">
                  <Dotty />
                </span>
              </p>
              <button
                v-for="(m, j) in memoryHits"
                :key="m.id"
                ref="memoryRowsEl"
                class="cp-row cp-row-memory"
                :class="{ 'cp-row-active': cursor === memoryBase + j }"
                role="option"
                :aria-selected="cursor === memoryBase + j"
                @mouseenter="cursor = memoryBase + j"
                @click="goMemory(m.id)"
              >
                <span class="cp-row-glyph" aria-hidden="true">·</span>
                <!-- v-html is safe: highlightMatches escapes both
                     source + query before inserting <mark> tags. -->
                <span class="cp-row-text" v-html="highlightedPreview(m)" />
                <span v-if="m.category" class="cp-row-meta">{{ m.category }}</span>
              </button>
            </template>

            <!-- Loading shimmer when memories query in flight + no results yet -->
            <template v-if="!filteredRoutes.length && !memoryHits.length && memoriesLoading">
              <p class="cp-section">Memories</p>
              <span class="folio-skel cp-skel-row" />
              <span class="folio-skel cp-skel-row" />
              <span class="folio-skel cp-skel-row short" />
            </template>

            <!-- Empty hint -->
            <div
              v-if="!filteredRoutes.length && !memoryHits.length && !memoriesLoading"
              class="cp-empty"
            >
              <p v-if="!query.trim()" class="cp-empty-headline">— type to seek the codex —</p>
              <p v-else class="cp-empty-headline">— this seeking returns nothing —</p>
              <p class="cp-empty-hint">
                <em>Routes match by label.</em>
                <span v-if="query.trim()"> Memories search the corpus by full-text.</span>
              </p>
            </div>
          </div>

          <footer class="cp-foot">
            <span class="cp-foot-key"><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
            <span class="cp-foot-key"><kbd>↵</kbd> open</span>
            <span class="cp-foot-key"><kbd>Esc</kbd> close</span>
            <span class="cp-foot-trigger"><kbd>⌘</kbd><kbd>K</kbd> · <kbd>Ctrl</kbd><kbd>K</kbd></span>
          </footer>
        </Motion>
      </Motion>
    </AnimatePresence>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { Motion, AnimatePresence } from 'motion-v'
import api from '@/api/client'
import Dotty from './Dotty.vue'
import { highlightMatches } from '@/composables/useHighlight'

interface RouteHit { to: string; label: string }
interface MemoryHit {
  id: string
  content?: string
  summary?: string
  category?: string
}

// Routes match the AppBar nav exactly — kept in sync there. If the
// nav changes, this list moves with it (or migrate both to a shared
// constant in a future PR).
const ROUTES: RouteHit[] = [
  { to: '/commonplace', label: 'Commonplace' },
  { to: '/insights',    label: 'Insights' },
  { to: '/memories',    label: 'Memories' },
  { to: '/galaxy',      label: 'Galaxy' },
  { to: '/graph',       label: 'Graph' },
  { to: '/timeline',    label: 'Timeline' },
  { to: '/scheduler',   label: 'Scheduler' },
  { to: '/pulse',       label: 'Pulse' },
  { to: '/threads',     label: 'Threads' },
]

const router = useRouter()
const open = ref(false)
const query = ref('')
const cursor = ref(0)
const inputEl = ref<HTMLInputElement | null>(null)
const listEl = ref<HTMLElement | null>(null)
const memoryHits = ref<MemoryHit[]>([])
const memoriesLoading = ref(false)
let searchTimer: number | undefined

// Persistent recent-queries history. Capped at 5 to keep the empty
// palette tight; ordered most-recent-first; deduped on insert.
// Storage key bumps if the shape ever changes substantively.
const HISTORY_KEY = 'nobrainr.cmdk.history.v1'
const HISTORY_MAX = 5
const recentQueries = ref<string[]>([])

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw)
    if (Array.isArray(parsed)) {
      recentQueries.value = parsed.filter((q): q is string => typeof q === 'string').slice(0, HISTORY_MAX)
    }
  } catch {
    // Storage disabled (Safari private mode etc.) — palette still
    // works, history just doesn't persist for the session.
  }
}

function saveHistory() {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(recentQueries.value))
  } catch { /* see loadHistory */ }
}

function rememberQuery(q: string) {
  const trimmed = q.trim()
  if (!trimmed) return
  recentQueries.value = [
    trimmed,
    ...recentQueries.value.filter((x) => x !== trimmed),
  ].slice(0, HISTORY_MAX)
  saveHistory()
}

function forgetQuery(q: string) {
  recentQueries.value = recentQueries.value.filter((x) => x !== q)
  saveHistory()
  // Pull cursor back if the removal pulled the selection out of bounds.
  cursor.value = Math.min(cursor.value, totalRows.value - 1)
}

function recallQuery(q: string) {
  query.value = q
  cursor.value = 0
  // Re-focus the input so the next keystroke continues editing.
  nextTick(() => inputEl.value?.focus())
}

const showRecent = computed(() => !query.value.trim() && recentQueries.value.length > 0)
const routeBase = computed(() => (showRecent.value ? recentQueries.value.length : 0))
const memoryBase = computed(() => routeBase.value + filteredRoutes.value.length)
const totalRows = computed(() => routeBase.value + filteredRoutes.value.length + memoryHits.value.length)

const filteredRoutes = computed<RouteHit[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return ROUTES // empty query → show all routes for fast nav
  return ROUTES.filter(
    (r) => r.label.toLowerCase().includes(q) || r.to.includes(q),
  )
})

watch(query, (q) => {
  cursor.value = 0
  if (searchTimer !== undefined) {
    clearTimeout(searchTimer)
    searchTimer = undefined
  }
  if (!q.trim()) {
    memoryHits.value = []
    memoriesLoading.value = false
    return
  }
  memoriesLoading.value = true
  // 200ms debounce — tighter than typical search (e.g. 300ms) because
  // /api/recall is FTS-only (no embedding call) and answers in <50ms.
  searchTimer = window.setTimeout(async () => {
    try {
      const { data } = await api.get<MemoryHit[]>('/api/recall', {
        params: { q: q.trim(), limit: 8 },
      })
      memoryHits.value = data ?? []
    } catch {
      memoryHits.value = []
    } finally {
      memoriesLoading.value = false
    }
  }, 200)
})

function moveCursor(delta: number) {
  const total = totalRows.value
  if (!total) return
  cursor.value = (cursor.value + delta + total) % total
  scrollCursorIntoView()
}

function scrollCursorIntoView() {
  nextTick(() => {
    const list = listEl.value
    if (!list) return
    const active = list.querySelector<HTMLElement>('.cp-row-active')
    active?.scrollIntoView({ block: 'nearest' })
  })
}

function activateCursor() {
  const i = cursor.value
  if (showRecent.value && i < recentQueries.value.length) {
    recallQuery(recentQueries.value[i])
    return
  }
  const r = i - routeBase.value
  if (r >= 0 && r < filteredRoutes.value.length) {
    goRoute(filteredRoutes.value[r].to)
    return
  }
  const m = memoryHits.value[i - memoryBase.value]
  if (m) goMemory(m.id)
}

function goRoute(to: string) {
  // Only remember the query if the user actually typed something.
  // Hitting Enter on an empty palette to jump to a default route
  // shouldn't pollute history with empty entries.
  rememberQuery(query.value)
  router.push(to)
  close()
}
function goMemory(id: string) {
  rememberQuery(query.value)
  // Land on Memories index; the scoped state on that route picks the
  // memory by id from the URL hash. Caveman: any agent that needs a
  // dedicated /memory/{id} route should add it later — for now Memories
  // accepts a `?id=` param via the existing useMemories composable.
  router.push({ path: '/memories', query: { id } })
  close()
}

function shortPreview(m: MemoryHit): string {
  const text = m.summary || m.content || ''
  return text.length <= 100 ? text : text.slice(0, 100).trimEnd() + '…'
}

function highlightedPreview(m: MemoryHit): string {
  return highlightMatches(shortPreview(m), query.value)
}

function openPalette() {
  open.value = true
  query.value = ''
  cursor.value = 0
  memoryHits.value = []
  nextTick(() => inputEl.value?.focus())
}

function close() {
  open.value = false
}

function onGlobalKey(e: KeyboardEvent) {
  // Cmd-K (mac) / Ctrl-K (linux + win) toggle.
  const cmdK = (e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')
  if (cmdK) {
    e.preventDefault()
    open.value ? close() : openPalette()
  }
}

onMounted(() => {
  loadHistory()
  window.addEventListener('keydown', onGlobalKey)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKey)
  if (searchTimer !== undefined) clearTimeout(searchTimer)
})
</script>

<style scoped>
.cp-shell {
  position: fixed;
  inset: 0;
  z-index: 300;
  display: grid;
  align-items: start;
  justify-items: center;
  padding-top: 12vh;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.cp-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(8, 6, 2, 0.62);
  backdrop-filter: blur(8px);
}

.cp-card {
  position: relative;
  width: min(640px, 92vw);
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1100px 700px at 50% 0%, rgba(200, 169, 110, 0.06), transparent 65%),
    linear-gradient(180deg, var(--cp-paper) 0%, var(--cp-paper-deep) 100%);
  border: 1px solid var(--cp-rule);
  border-radius: 4px;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.6);
  color: var(--cp-ink);
  overflow: hidden;
}

.cp-head {
  padding: 22px 26px 14px;
  text-align: center;
}
.cp-eyebrow {
  display: inline-block;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  margin-bottom: 8px;
}
.cp-title {
  font-size: 22px;
  font-weight: 400;
  margin: 0 0 4px;
  font-variant: small-caps;
  letter-spacing: -0.005em;
}
.cp-tagline {
  margin: 0;
  font-size: 13.5px;
  color: var(--cp-ink-mute);
  font-style: italic;
}

.cp-input-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 12px;
  margin: 4px 22px 4px;
  padding: 8px 12px;
  border: 1px solid var(--cp-rule);
  border-radius: 3px;
  background: var(--cp-gold-trace);
}
.cp-input-glyph {
  color: var(--cp-gold);
  font-size: 14px;
}
.cp-input {
  background: transparent;
  border: none;
  color: var(--cp-ink);
  font-family: inherit;
  font-size: 17px;
  letter-spacing: 0.01em;
  outline: none;
  width: 100%;
  padding: 2px 0;
}
.cp-input::placeholder {
  color: var(--cp-ink-faint);
  font-style: italic;
}
.cp-input-kbd {
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 10px;
  color: var(--cp-ink-mute);
  border: 1px solid var(--cp-rule);
  border-radius: 2px;
  padding: 1px 5px;
}

.cp-list {
  flex: 1 1 0;
  overflow-y: auto;
  padding: 8px 8px 12px;
}
.cp-section {
  margin: 8px 14px 4px;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  display: flex;
  align-items: center;
  gap: 8px;
}
.cp-section-count {
  text-transform: none;
  letter-spacing: 0.04em;
  color: var(--cp-gold-faint);
  font-variant-numeric: tabular-nums;
}
.cp-section-loading { margin-left: auto; }

.cp-row {
  width: calc(100% - 16px);
  margin: 0 8px 1px;
  padding: 9px 12px;
  display: grid;
  align-items: baseline;
  gap: 12px;
  background: transparent;
  border: 0;
  border-left: 2px solid transparent;
  border-radius: 2px;
  text-align: left;
  font-family: inherit;
  color: var(--cp-ink);
  cursor: pointer;
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    border-left-color var(--cp-dur-hover) var(--cp-ease),
    transform var(--cp-dur-hover) var(--cp-ease);
}
.cp-row-route { grid-template-columns: 16px auto 1fr; }
.cp-row-memory { grid-template-columns: 16px 1fr auto; }
.cp-row-recent { grid-template-columns: 16px 1fr auto; }
.cp-row-recent .cp-row-text {
  font-style: italic;
  color: var(--cp-ink-mute);
}
.cp-row-recent:hover .cp-row-text,
.cp-row-recent.cp-row-active .cp-row-text {
  color: var(--cp-ink);
}
.cp-row-recent-dismiss {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 2px;
  color: var(--cp-ink-faint);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  transition:
    color var(--cp-dur-hover) var(--cp-ease),
    background var(--cp-dur-hover) var(--cp-ease);
}
.cp-row-recent-dismiss:hover {
  color: var(--cp-ink);
  background: var(--cp-gold-trace);
}

.cp-row:hover,
.cp-row-active {
  background: var(--cp-gold-trace);
  border-left-color: var(--cp-gold);
  transform: translateX(2px);
}

.cp-row-glyph {
  color: var(--cp-gold-soft);
  font-style: italic;
  font-size: 14.5px;
}
.cp-row-label {
  font-size: 15.5px;
  letter-spacing: 0.04em;
}
.cp-row-path {
  font-style: italic;
  font-size: 12.5px;
  color: var(--cp-ink-faint);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.cp-row-text {
  font-size: 14.5px;
  line-height: 1.45;
  color: var(--cp-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.cp-row-meta {
  font-style: italic;
  font-size: 11.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  border: 1px solid var(--cp-gold-faint);
  padding: 1px 6px;
  border-radius: 2px;
}

.cp-empty { padding: 28px 22px; text-align: center; }
.cp-empty-headline {
  margin: 0 0 6px;
  font-style: italic;
  color: var(--cp-ink-mute);
}
.cp-empty-hint {
  margin: 0;
  font-style: italic;
  font-size: 13.5px;
  color: var(--cp-ink-faint);
  line-height: 1.5;
}

.cp-skel-row {
  display: block;
  height: 14px;
  margin: 6px 14px;
  width: calc(100% - 28px);
}
.cp-skel-row.short { width: 64%; }

.cp-foot {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 18px;
  border-top: 1px solid var(--cp-rule);
  background: rgba(8, 6, 2, 0.3);
  font-style: italic;
  font-size: 12.5px;
  color: var(--cp-ink-mute);
}
.cp-foot kbd {
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-style: normal;
  font-size: 10px;
  color: var(--cp-gold-soft);
  border: 1px solid var(--cp-rule);
  border-radius: 2px;
  padding: 1px 5px;
  margin: 0 1px;
}
.cp-foot-key { display: inline-flex; align-items: center; gap: 4px; }
.cp-foot-trigger { margin-left: auto; }

@media (prefers-reduced-motion: reduce) {
  .cp-row { transition: none !important; }
  .cp-row:hover, .cp-row-active { transform: none; }
}

/* Phone tweaks: card narrower, less top offset, footer hides keys
   that don't apply on touch. */
@media (max-width: 480px) {
  .cp-shell { padding-top: 6vh; }
  .cp-card { width: 96vw; }
  .cp-foot-key { display: none; }
  .cp-foot-trigger { margin-left: 0; }
}
</style>
