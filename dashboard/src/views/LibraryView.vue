<template>
  <v-container fluid class="fill-height pa-0 lib-shell" :data-pane="mobilePane">
    <div class="d-flex fill-height lib-cols" style="width: 100%;">

      <!-- ══ Catalog rail — the card drawer ══ -->
      <div class="catalog-panel d-flex flex-column">
        <div class="pa-3 pb-2">
          <div class="folio-heading mb-1 d-flex align-center justify-space-between">
            <div class="d-flex align-center">
              <v-icon icon="mdi-bookshelf" size="16" class="mr-1" style="color: var(--cp-gold);" />
              <span class="lib-title-caps">Library</span>
            </div>
            <span class="lib-count" v-if="!loadingDocs">{{ documents.length }} works</span>
          </div>
          <div class="lib-subtitle mb-3">studies · memos · notes — the sources themselves</div>
          <v-text-field
            v-model="catalogQuery"
            prepend-inner-icon="mdi-magnify"
            placeholder="Find a document…"
            clearable
            density="compact"
            variant="outlined"
            hide-details
            class="cp-search"
          />
        </div>
        <v-divider style="opacity: 0.15; border-color: var(--cp-gold);" />

        <div class="flex-grow-1 catalog-scroll">
          <template v-if="loadingDocs">
            <div v-for="n in 9" :key="n" class="folio-skel catalog-skel mx-3 mt-2" />
          </template>
          <template v-else>
            <template v-for="group in groupedDocs" :key="group.type">
              <div class="shelf-rule mx-3 mt-4 mb-1">
                <span class="shelf-label">{{ group.label }}</span>
                <span class="shelf-count">{{ group.docs.length }}</span>
              </div>
              <button
                v-for="(doc, i) in group.docs"
                :key="doc.ref"
                class="spine"
                :class="{ 'spine--open': doc.ref === openRef }"
                :style="{ '--reveal-i': Math.min(i, 14) }"
                @click="openDocument(doc.ref)"
              >
                <span class="spine-title">{{ prettyRef(doc.ref) }}</span>
                <span class="spine-meta">
                  {{ doc.chunks }} {{ doc.chunks === 1 ? 'folio' : 'folia' }}
                  · {{ kchars(doc.chars) }}
                </span>
              </button>
            </template>
            <div v-if="!documents.length" class="pa-6 text-center">
              <div class="lib-empty-glyph">∅</div>
              <div class="lib-empty-text">No works match. The catalog holds only what was imported — widen the search.</div>
            </div>
          </template>
        </div>
      </div>

      <!-- ══ Reading surface ══ -->
      <div class="reader-panel flex-grow-1 d-flex flex-column">

        <!-- Un-opened state: the inquiry desk -->
        <div v-if="!openRef" class="desk d-flex flex-column align-center justify-center flex-grow-1">
          <div class="desk-glyph">❦</div>
          <div class="desk-line">Ask across all {{ documents.length || '—' }} works, or open one from the catalog.</div>
          <div class="inquiry inquiry--desk mt-5">
            <input
              v-model="askQuery"
              class="inquiry-input"
              placeholder="Ask your library — “what do my notes say about …”"
              @keydown.enter="ask(null)"
            />
            <button class="inquiry-go" :disabled="asking" @click="ask(null)">
              <span v-if="!asking">ask</span>
              <span v-else class="dotty"><span>·</span><span>·</span><span>·</span></span>
            </button>
          </div>
          <div v-if="answer" class="annotation annotation--desk mt-6">
            <div class="annotation-rule" />
            <p class="annotation-text">{{ answer.text }}</p>
            <div class="annotation-cites">
              <button
                v-for="c in answer.citations" :key="c.idx"
                class="cite-chip"
                @click="openDocument(c.document)"
              >[{{ c.idx }}] {{ prettyRef(c.document) }}</button>
            </div>
          </div>
        </div>

        <!-- Opened document -->
        <template v-else>
          <div class="reader-head px-5 pt-4 pb-3">
            <div class="d-flex align-center justify-space-between flex-wrap">
              <div class="min-w-0">
                <div class="reader-title">{{ prettyRef(openRef) }}</div>
                <div class="reader-meta">
                  {{ docChunks.length }} folia
                  <template v-if="originalUrl"> · <a class="reader-original" :href="originalUrl" target="_blank" rel="noopener">original ↗</a></template>
                  <template v-else> · <span class="reader-path" :title="'Original in Nextcloud: ' + openRef">{{ openRef }}</span></template>
                </div>
              </div>
              <button class="reader-close" @click="closeDocument" title="Back to catalog">✕</button>
            </div>

            <!-- inquiry line, scoped to this work -->
            <div class="inquiry mt-3">
              <input
                v-model="askQuery"
                class="inquiry-input"
                :placeholder="`Ask this document…`"
                @keydown.enter="ask(openRef)"
              />
              <button class="inquiry-go" :disabled="asking" @click="ask(openRef)">
                <span v-if="!asking">ask</span>
                <span v-else class="dotty"><span>·</span><span>·</span><span>·</span></span>
              </button>
              <input
                v-model="withinQuery"
                class="inquiry-input inquiry-input--find"
                placeholder="find on page…"
              />
            </div>

            <div v-if="answer" class="annotation mt-3">
              <div class="annotation-rule" />
              <p class="annotation-text">{{ answer.text }}</p>
              <div class="annotation-cites">
                <button
                  v-for="c in answer.citations" :key="c.idx"
                  class="cite-chip"
                  @click="flashChunk(c.chunkId)"
                >[{{ c.idx }}]</button>
              </div>
            </div>
          </div>
          <v-divider style="opacity: 0.15; border-color: var(--cp-gold);" />

          <div class="folio-scroll flex-grow-1" ref="folioScroll">
            <template v-if="loadingDoc">
              <div v-for="n in 5" :key="n" class="folio-skel folio-skel--para mx-6 mt-4" />
            </template>
            <article v-else class="folio-body">
              <section
                v-for="(chunk, i) in docChunks"
                :key="chunk.id"
                :id="'folio-' + chunk.id"
                class="folio"
                :class="{ 'folio--flash': flashedId === chunk.id }"
                :style="{ '--reveal-i': Math.min(i, 8) }"
              >
                <span class="folio-no" :title="chunk.summary || ''">{{ roman(i + 1) }}</span>
                <p class="folio-text" v-html="highlight(chunk.content)"></p>
              </section>
            </article>
          </div>
        </template>
      </div>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import api from '@/api/client'

interface LibDoc { ref: string; source_type: string; chunks: number; chars: number; first_summary: string | null; original_url: string | null }
interface Chunk { id: string; content: string; summary: string | null; trust_score: number | null }

const documents = ref<LibDoc[]>([])
const loadingDocs = ref(true)
const catalogQuery = ref('')
const openRef = ref<string | null>(null)
const originalUrl = ref<string | null>(null)
const docChunks = ref<Chunk[]>([])
const loadingDoc = ref(false)
const withinQuery = ref('')
const askQuery = ref('')
const asking = ref(false)
const answer = ref<{ text: string; citations: { idx: number; chunkId: string; document: string }[] } | null>(null)
const flashedId = ref<string | null>(null)
const folioScroll = ref<HTMLElement | null>(null)
const mobilePane = computed(() => (openRef.value ? 'reader' : 'catalog'))

const SHELF_LABELS: Record<string, string> = {
  docx: 'Studies & Documents',
  affine_memos: 'Affine Memos',
  markdown_notes: 'Notes',
}

const groupedDocs = computed(() => {
  const q = catalogQuery.value.trim().toLowerCase()
  const filtered = q
    ? documents.value.filter(d => d.ref.toLowerCase().includes(q))
    : documents.value
  const by: Record<string, LibDoc[]> = {}
  for (const d of filtered) (by[d.source_type] ||= []).push(d)
  return Object.entries(by)
    .map(([type, docs]) => ({ type, label: SHELF_LABELS[type] || type, docs }))
    .sort((a, b) => b.docs.length - a.docs.length)
})

function prettyRef(ref: string): string {
  return ref.replace(/\.(docx?|md|txt)$/i, '').replace(/[_]+/g, ' ')
}
function kchars(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`
}
const ROMANS: [number, string][] = [[1000, 'M'], [900, 'CM'], [500, 'D'], [400, 'CD'], [100, 'C'], [90, 'XC'], [50, 'L'], [40, 'XL'], [10, 'X'], [9, 'IX'], [5, 'V'], [4, 'IV'], [1, 'I']]
function roman(n: number): string {
  let out = ''
  for (const [v, s] of ROMANS) while (n >= v) { out += s; n -= v }
  return out
}
function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
function highlight(content: string): string {
  const safe = escapeHtml(content)
  const q = withinQuery.value.trim()
  if (q.length < 2) return safe
  const rx = new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
  return safe.replace(rx, '<mark class="folio-mark">$1</mark>')
}

async function loadCatalog() {
  loadingDocs.value = true
  try {
    const { data } = await api.get('/api/library')
    documents.value = data.documents
  } finally {
    loadingDocs.value = false
  }
}

async function openDocument(ref: string) {
  openRef.value = ref
  answer.value = null
  loadingDoc.value = true
  try {
    const { data } = await api.get('/api/library/doc', { params: { ref } })
    docChunks.value = data.chunks
    originalUrl.value = data.original_url
  } finally {
    loadingDoc.value = false
  }
}
function closeDocument() {
  openRef.value = null
  docChunks.value = []
  answer.value = null
  withinQuery.value = ''
}

async function ask(scopeRef: string | null) {
  const q = askQuery.value.trim()
  if (!q || asking.value) return
  asking.value = true
  answer.value = null
  try {
    const { data } = await api.get('/api/library/search', {
      params: { q, ref: scopeRef || '', limit: 6 },
    })
    // Fast path (no LLM): compose an extractive answer from top hits —
    // the cited chunks ARE the answer surface; synthesis stays on the
    // MCP tool for agents. Fidelity beats fluency here too.
    const hits = data.hits || []
    if (!hits.length) {
      answer.value = { text: 'Nothing in the library speaks to that. Try other words — or it was never written down.', citations: [] }
      return
    }
    const lead = (hits[0].summary || hits[0].content || '').slice(0, 260)
    answer.value = {
      text: lead + (lead.length >= 260 ? '…' : ''),
      citations: hits.slice(0, 6).map((h: any, i: number) => ({
        idx: i + 1,
        chunkId: h.id,
        document: (h.metadata && h.metadata.file_path) || h.source_ref || '',
      })),
    }
  } finally {
    asking.value = false
  }
}

async function flashChunk(chunkId: string) {
  if (!docChunks.value.some(c => c.id === chunkId)) return
  await nextTick()
  const el = document.getElementById('folio-' + chunkId)
  if (el && folioScroll.value) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    flashedId.value = chunkId
    setTimeout(() => (flashedId.value = null), 1600)
  }
}

watch(withinQuery, () => { /* re-render via computed highlight */ })

loadCatalog()
</script>

<style scoped>
/* ═══ The Reading Room — codex palette, asymmetric two-pane ═══ */

.lib-shell {
  background:
    radial-gradient(1200px 500px at 85% -10%, var(--cp-gold-trace), transparent 60%),
    var(--cp-paper-deep);
}

/* Catalog rail — a card drawer, not a card grid */
.catalog-panel {
  width: 340px;
  min-width: 340px;
  border-right: 1px solid var(--cp-rule);
  background: var(--cp-paper);
}
.catalog-scroll { overflow-y: auto; padding-bottom: 24px; }

.lib-title-caps {
  font-family: Georgia, Palatino, serif;
  font-size: 13px;
  color: var(--cp-gold);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.lib-subtitle {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-faint);
}
.lib-count {
  font-size: 10px;
  color: var(--cp-gold-soft);
  font-variant-numeric: tabular-nums;
}

/* Shelf rules between source groups */
.shelf-rule {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 1px solid var(--cp-rule);
  padding-bottom: 3px;
}
.shelf-label {
  font-family: Georgia, serif;
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
}
.shelf-count { font-size: 10px; color: var(--cp-ink-faint); font-variant-numeric: tabular-nums; }

/* Spines — full-width text buttons, staggered reveal */
.spine {
  display: block;
  width: calc(100% - 24px);
  margin: 0 12px;
  padding: 7px 10px 7px 14px;
  text-align: left;
  border: 0;
  border-left: 2px solid transparent;
  background: transparent;
  cursor: pointer;
  opacity: 0;
  transform: translateY(4px);
  animation: spine-in 320ms var(--cp-ease-decel) forwards;
  animation-delay: calc(var(--reveal-i) * 28ms);
  transition: background var(--cp-dur-hover) var(--cp-ease),
              border-color var(--cp-dur-hover) var(--cp-ease);
}
@keyframes spine-in { to { opacity: 1; transform: translateY(0); } }
.spine:hover { background: var(--cp-gold-trace); }
.spine--open {
  border-left-color: var(--cp-gold);
  background: var(--cp-gold-trace);
}
.spine-title {
  display: block;
  font-family: Georgia, Palatino, serif;
  font-size: 13.5px;
  line-height: 1.35;
  color: var(--cp-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spine-meta {
  display: block;
  margin-top: 1px;
  font-size: 10px;
  color: var(--cp-ink-faint);
  font-variant-numeric: tabular-nums;
}

.catalog-skel { height: 34px; border-radius: 3px; }

/* Empty catalog */
.lib-empty-glyph { font-family: Georgia, serif; font-size: 32px; color: var(--cp-gold); opacity: 0.12; }
.lib-empty-text { font-family: Georgia, serif; font-style: italic; font-size: 12px; color: var(--cp-ink-faint); max-width: 220px; margin: 8px auto 0; }

/* ═══ Reading surface ═══ */
.reader-panel { min-width: 0; }

/* The desk (nothing open): centered inquiry */
.desk-glyph { font-family: Georgia, serif; font-size: 40px; color: var(--cp-gold); opacity: 0.16; }
.desk-line {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  color: var(--cp-ink-mute);
  margin-top: 6px;
}

/* Inquiry line — a rule with a cursor, not a boxed form */
.inquiry {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 720px;
}
.inquiry--desk { width: min(560px, 82%); }
.inquiry-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--cp-rule);
  padding: 6px 2px;
  font-family: Georgia, Palatino, serif;
  font-size: 14px;
  color: var(--cp-ink);
  transition: border-color var(--cp-dur-hover) var(--cp-ease);
}
.inquiry-input::placeholder { color: var(--cp-ink-faint); font-style: italic; }
.inquiry-input:focus { border-bottom-color: var(--cp-gold-soft); box-shadow: none; }
.inquiry-input--find { flex: 0 1 180px; font-size: 12px; }
.inquiry-go {
  border: 1px solid var(--cp-gold-faint);
  background: transparent;
  color: var(--cp-gold);
  font-family: Georgia, serif;
  font-size: 12px;
  letter-spacing: 0.1em;
  padding: 4px 14px;
  border-radius: 2px;
  cursor: pointer;
  transition: background var(--cp-dur-hover) var(--cp-ease), border-color var(--cp-dur-hover) var(--cp-ease);
}
.inquiry-go:hover:not(:disabled) { background: var(--cp-gold-trace); border-color: var(--cp-gold-soft); }
.inquiry-go:disabled { opacity: 0.6; cursor: default; }

/* Answer annotation — a marginal note, not a chat bubble */
.annotation { max-width: 720px; }
.annotation--desk { width: min(560px, 82%); }
.annotation-rule { width: 42px; border-top: 1px solid var(--cp-gold-soft); margin-bottom: 8px; }
.annotation-text {
  font-family: Georgia, Palatino, serif;
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--cp-ink);
  margin: 0;
}
.annotation-cites { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.cite-chip {
  border: 0;
  background: transparent;
  color: var(--cp-gold);
  font-size: 11px;
  font-family: Georgia, serif;
  cursor: pointer;
  padding: 1px 4px;
  border-radius: 2px;
  transition: background var(--cp-dur-hover) var(--cp-ease);
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cite-chip:hover { background: var(--cp-gold-trace); }

/* Reader head */
.reader-title {
  font-family: Georgia, Palatino, serif;
  font-size: 19px;
  color: var(--cp-ink);
  line-height: 1.3;
}
.reader-meta { font-size: 11px; color: var(--cp-ink-faint); margin-top: 2px; }
.reader-original { color: var(--cp-gold); text-decoration: none; }
.reader-original:hover { text-decoration: underline; }
.reader-path { font-style: italic; }
.reader-close {
  border: 0;
  background: transparent;
  color: var(--cp-ink-faint);
  font-size: 14px;
  cursor: pointer;
  padding: 4px 8px;
  transition: color var(--cp-dur-hover) var(--cp-ease);
}
.reader-close:hover { color: var(--cp-gold); }

/* Folia — continuous text with roman marginalia */
.folio-scroll { overflow-y: auto; }
.folio-body {
  max-width: 720px;
  padding: 20px 28px 80px 64px;
}
.folio {
  position: relative;
  opacity: 0;
  transform: translateY(6px);
  animation: spine-in 380ms var(--cp-ease-decel) forwards;
  animation-delay: calc(var(--reveal-i) * 45ms);
  border-radius: 3px;
  transition: background 600ms var(--cp-ease);
}
.folio--flash { background: var(--cp-gold-faint); }
.folio-no {
  position: absolute;
  left: -46px;
  top: 4px;
  width: 36px;
  text-align: right;
  font-family: Georgia, serif;
  font-size: 10.5px;
  color: var(--cp-gold-soft);
  letter-spacing: 0.06em;
  user-select: none;
}
.folio-text {
  font-family: Georgia, Palatino, serif;
  font-size: 14px;
  line-height: 1.75;
  color: var(--cp-ink);
  white-space: pre-wrap;
  margin: 0 0 22px;
}
:deep(.folio-mark) {
  background: var(--cp-gold-faint);
  color: var(--cp-gold-bright);
  border-radius: 2px;
  padding: 0 1px;
}
.folio-skel--para { height: 72px; border-radius: 3px; }

/* ═══ Mobile: catalog-first, reader replaces ═══ */
@media (max-width: 860px) {
  .lib-cols { flex-direction: column; }
  .catalog-panel { width: 100%; min-width: 0; border-right: 0; }
  .lib-shell[data-pane='reader'] .catalog-panel { display: none; }
  .lib-shell[data-pane='catalog'] .reader-panel { display: none; }
  .folio-body { padding-left: 48px; }
  .folio-no { left: -38px; }
}

@media (prefers-reduced-motion: reduce) {
  .spine, .folio { animation: none; opacity: 1; transform: none; }
}
</style>
