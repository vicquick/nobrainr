<template>
  <div class="threads-page">
    <div class="threads-shell">

      <!-- MASTHEAD -->
      <header class="threads-masthead">
        <div class="masthead-rule" />
        <div class="masthead-inner">
          <div class="masthead-row">
            <span class="folio-label">Codex Conversationum</span>
            <span class="thread-count" v-if="threads.length">
              <em>{{ threads.length }}</em> {{ threads.length === 1 ? 'thread' : 'threads' }}
            </span>
          </div>
          <h1 class="threads-title">Threads</h1>
          <p class="threads-tagline">
            Raw conversations preserved as written — the unbound, undistilled archive.
          </p>
        </div>
        <div class="masthead-rule" />
      </header>

      <!-- CONTROLS -->
      <div class="threads-controls">
        <div class="search-row">
          <span class="search-glyph">⚹</span>
          <input
            v-model="query"
            class="folio-input"
            type="text"
            placeholder="search the conversations…"
            @keyup.enter="search"
          />
        </div>
        <select v-model="sourceFilter" class="folio-select" @change="search">
          <option :value="null">All sources</option>
          <option value="chatgpt">ChatGPT</option>
          <option value="claude_web">Claude</option>
        </select>
        <button class="folio-button" :disabled="loading" @click="search">
          <span v-if="loading" class="dotty">·  ·  ·</span>
          <span v-else>seek</span>
        </button>
      </div>

      <!-- LIST -->
      <div v-if="loading" class="threads-loading">
        <span class="dotty">·  ·  ·</span>
        <p class="loading-text">opening the codex</p>
      </div>
      <div v-else-if="!threads.length" class="threads-empty">
        <span class="ornament">❦</span>
        <p>{{ query ? '— no thread answers to this seeking —' : '— recent threads will appear here —' }}</p>
      </div>
      <ul v-else class="threads-list">
        <li
          v-for="(t, i) in threads"
          :key="t.id"
          class="thread-line"
          @click="openThread(t.id)"
        >
          <span class="thread-numeral">{{ toRoman(i + 1) }}.</span>
          <div class="thread-body">
            <p class="thread-title">{{ t.title || '(untitled)' }}</p>
            <p class="thread-meta">
              <em>{{ t.source_type }}</em>
              <span class="dot">·</span>
              <span>{{ t.message_count || 0 }} messages</span>
              <span class="dot">·</span>
              <span>{{ formatDate(t.imported_at) }}</span>
            </p>
          </div>
          <span class="thread-arrow">›</span>
        </li>
      </ul>

    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'

interface Thread {
  id: string
  source_type?: string
  title?: string
  message_count?: number
  imported_at?: string
}

const router = useRouter()
const query = ref('')
const sourceFilter = ref<string | null>(null)
const threads = ref<Thread[]>([])
const loading = ref(false)

async function search() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (query.value) params.set('q', query.value)
    if (sourceFilter.value) params.set('source_type', sourceFilter.value)
    params.set('limit', '50')
    const r = await fetch(`/api/conversations?${params.toString()}`)
    threads.value = await r.json()
  } finally {
    loading.value = false
  }
}

function openThread(id: string) {
  router.push(`/threads/${id}`)
}

function formatDate(s?: string) {
  if (!s) return ''
  return new Date(s).toLocaleDateString('en-GB', {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

function toRoman(n: number): string {
  const r: [number, string][] = [
    [100, 'C'], [90, 'XC'], [50, 'L'], [40, 'XL'],
    [10, 'X'], [9, 'IX'], [5, 'V'], [4, 'IV'], [1, 'I'],
  ]
  let out = ''
  for (const [v, s] of r) { while (n >= v) { out += s; n -= v } }
  return out
}

onMounted(search)
</script>

<style scoped>
.threads-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 32px 24px 80px;
  min-height: 100vh;
}

.threads-shell { max-width: 820px; margin: 0 auto; }

.threads-masthead { margin-bottom: 32px; }
.masthead-rule {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--cp-gold-soft) 30%, var(--cp-gold) 50%, var(--cp-gold-soft) 70%, transparent);
}
.masthead-inner { padding: 16px 0 12px; text-align: center; }
.masthead-row {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 11px; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--cp-ink-mute);
  margin-bottom: 12px;
}
.folio-label {
  font-style: italic; letter-spacing: 0.22em;
  color: var(--cp-gold); font-size: 10px;
}
.thread-count {
  font-style: italic; font-size: 11px;
  color: var(--cp-ink-mute); letter-spacing: 0.05em;
}
.thread-count em {
  color: var(--cp-ink); font-style: normal;
  font-variant-numeric: tabular-nums;
}
.threads-title {
  font-family: Georgia, serif;
  font-size: clamp(34px, 4.5vw, 48px);
  font-weight: 400; letter-spacing: 0.02em;
  margin: 0 0 4px; color: var(--cp-ink);
}
.threads-tagline {
  font-style: italic; color: var(--cp-ink-mute);
  font-size: 14px; margin: 0 0 16px;
}

/* CONTROLS */
.threads-controls {
  display: grid;
  grid-template-columns: 1fr 200px auto;
  gap: 14px;
  margin-bottom: 32px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--cp-gold-faint);
  align-items: center;
}
.search-row { position: relative; }
.search-glyph {
  position: absolute; left: 0; top: 50%;
  transform: translateY(-50%);
  color: var(--cp-gold-soft); font-size: 14px;
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
.folio-input::placeholder { color: var(--cp-ink-mute); }
.folio-input:focus { outline: none; border-bottom-color: var(--cp-gold); }

.folio-select {
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  padding: 6px 22px 6px 10px;
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
.folio-select option { background: #14110a; color: var(--cp-ink); }

.folio-button {
  background: transparent;
  border: 1px solid var(--cp-gold-soft);
  color: var(--cp-gold);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  letter-spacing: 0.1em;
  padding: 6px 22px;
  cursor: pointer;
  transition: all 200ms;
}
.folio-button:hover:not(:disabled) {
  background: rgba(200, 169, 110, 0.08);
  border-color: var(--cp-gold);
}
.folio-button:disabled { opacity: 0.5; cursor: wait; }

/* LIST */
.threads-list {
  list-style: none; padding: 0; margin: 0;
}
.thread-line {
  display: grid;
  grid-template-columns: 60px 1fr 20px;
  gap: 12px;
  padding: 16px 8px;
  border-bottom: 1px dotted var(--cp-gold-faint);
  cursor: pointer;
  align-items: baseline;
  transition: all 180ms cubic-bezier(0.22, 1, 0.36, 1);
}
.thread-line:hover {
  padding-left: 14px;
  background: rgba(200, 169, 110, 0.03);
}
.thread-numeral {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 16px;
  color: var(--cp-gold);
  text-align: right;
  letter-spacing: 0.05em;
}
.thread-title {
  font-family: Georgia, serif;
  font-size: 15px;
  color: var(--cp-ink);
  margin: 0 0 4px;
  letter-spacing: 0.02em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.thread-meta {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink-mute);
  margin: 0;
  display: flex;
  gap: 6px;
  align-items: baseline;
}
.thread-meta em { color: var(--cp-gold); font-style: italic; }
.dot { color: var(--cp-gold-soft); }
.thread-arrow {
  color: var(--cp-gold-soft);
  font-size: 18px;
  transition: all 180ms;
}
.thread-line:hover .thread-arrow {
  color: var(--cp-gold);
  transform: translateX(3px);
}

/* STATES */
.threads-loading, .threads-empty {
  text-align: center;
  padding: 80px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
}
.ornament {
  display: block;
  font-size: 24px;
  color: var(--cp-gold-soft);
  margin-bottom: 12px;
}
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }
.loading-text { font-size: 13px; margin: 8px 0 0; letter-spacing: 0.05em; }

/* MOBILE: stack the controls, no overflow on the list */
@media (max-width: 720px) {
  .threads-page { padding: 24px 14px 64px; }
  .threads-title { font-size: 30px; }

  /* Stack search + filter + button on their own rows */
  .threads-controls {
    grid-template-columns: 1fr;
    gap: 10px;
    margin-bottom: 24px;
  }
  .folio-button { width: 100%; padding: 8px 16px; }

  .thread-line {
    grid-template-columns: 36px 1fr 16px;
    gap: 8px;
    padding: 12px 4px;
  }
  .thread-title { font-size: 14px; }
  .thread-meta { font-size: 11px; flex-wrap: wrap; }
}
</style>
