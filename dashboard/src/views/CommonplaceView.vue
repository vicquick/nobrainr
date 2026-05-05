<template>
  <v-container fluid class="fill-height pa-0">
    <div class="d-flex fill-height" style="width: 100%;">

      <!-- Chapter list (left) -->
      <div class="chapters-panel d-flex flex-column" style="width: 300px; min-width: 300px;">
        <div class="pa-3 pb-2">
          <div class="folio-heading mb-3 d-flex align-center justify-space-between">
            <div class="d-flex align-center">
              <v-icon icon="mdi-book-open-page-variant" size="16" class="mr-1" style="color: var(--cp-gold);" />
              <span style="font-family: Georgia, Palatino, serif; font-size: 13px; color: var(--cp-gold); letter-spacing: 0.08em; text-transform: uppercase;">Commonplace Book</span>
            </div>
            <span v-if="searchMode" style="font-size: 10px; color: var(--cp-gold); opacity: 0.55; font-family: Georgia, serif; font-style: italic;">
              {{ searchHits.length }} hits
            </span>
          </div>
          <v-text-field
            v-model="searchQuery"
            prepend-inner-icon="mdi-magnify"
            placeholder="Search themes…"
            clearable
            density="compact"
            variant="outlined"
            hide-details
            class="cp-search"
            @update:model-value="onSearch"
          />
        </div>
        <v-divider style="opacity: 0.15; border-color: var(--cp-gold);" />
        <div class="flex-grow-1" style="overflow-y: auto;">
          <template v-if="loadingChapters">
            <div v-for="n in 8" :key="n" class="chapter-skeleton mx-3 mt-2" />
          </template>
          <template v-else>
            <div
              v-for="(ch, idx) in chapters"
              :key="ch.community_id"
              class="chapter-item px-3 py-2"
              :class="{ 'chapter-active': selectedChapter?.community_id === ch.community_id }"
              @click="selectChapter(ch)"
            >
              <div class="d-flex align-start ga-2">
                <span class="chapter-numeral text-caption" style="min-width: 22px; padding-top: 1px;">{{ toRoman(idx + 1) }}.</span>
                <div class="flex-grow-1 min-w-0">
                  <div class="chapter-title text-body-2 font-weight-medium">{{ ch.title }}</div>
                  <div class="d-flex align-center ga-2 mt-1">
                    <span v-if="searchMode" class="cp-hit-badge">{{ ch.hit_count }} hit{{ ch.hit_count === 1 ? '' : 's' }}</span>
                    <span v-else class="text-caption" style="color: var(--cp-muted);">{{ ch.memory_count.toLocaleString() }} entries</span>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="!chapters.length" class="text-center pa-6 text-medium-emphasis text-caption">
              No themes found
            </div>
          </template>
        </div>
      </div>

      <!-- Entry list (middle) -->
      <div
        class="entries-panel d-flex flex-column"
        style="width: 360px; min-width: 360px; border-left: 1px solid rgba(255,255,255,0.04); border-right: 1px solid rgba(255,255,255,0.04);"
      >
        <template v-if="selectedChapter">
          <div class="pa-3 pb-2">
            <div class="folio-chapter-header mb-1">
              <span class="chapter-title-large">{{ selectedChapter.title }}</span>
            </div>
            <div class="d-flex flex-wrap ga-1 mb-2">
              <v-chip
                v-for="topic in selectedChapter.key_topics"
                :key="topic"
                size="x-small"
                variant="tonal"
                class="cp-topic-chip"
              >{{ topic }}</v-chip>
            </div>
            <p v-if="searchMode" class="text-caption chapter-summary" style="color: rgba(200,169,110,0.5); font-style: italic;">{{ entries.length }} result{{ entries.length === 1 ? '' : 's' }} in this chapter</p>
            <p v-else class="text-caption chapter-summary">{{ selectedChapter.summary }}</p>
          </div>
          <v-divider style="opacity: 0.15; border-color: var(--cp-gold);" />
          <div class="flex-grow-1" style="overflow-y: auto;">
            <template v-if="loadingEntries">
              <div v-for="n in 6" :key="n" class="entry-skeleton mx-3 mt-2" />
            </template>
            <template v-else>
              <div
                v-for="m in entries"
                :key="m.id"
                class="entry-item px-3 py-2"
                :class="{ 'entry-active': selectedEntry?.id === m.id }"
                @click="selectEntry(m)"
              >
                <div class="entry-summary text-body-2">{{ m.summary || m.content?.slice(0, 80) || '—' }}</div>
                <div class="d-flex align-center ga-2 mt-1">
                  <span class="text-caption" style="color: var(--cp-muted);">{{ m.source_type || 'unknown' }}</span>
                  <span v-if="searchMode && m.rrf_score" class="cp-rrf-badge">{{ (m.rrf_score * 3000).toFixed(0) }}</span>
                  <span v-else-if="m.importance" class="text-caption" style="color: var(--cp-gold); opacity: 0.6;">
                    ★ {{ (m.importance * 100).toFixed(0) }}
                  </span>
                  <v-chip
                    v-if="m.category"
                    size="x-small"
                    variant="text"
                    style="color: var(--cp-muted); font-size: 10px;"
                  >{{ m.category }}</v-chip>
                </div>
              </div>
              <div v-if="!entries.length" class="text-center pa-8 text-medium-emphasis text-caption">
                No entries in this chapter
              </div>
            </template>
          </div>
        </template>
        <div v-else class="d-flex align-center justify-center fill-height">
          <div class="text-center pa-6">
            <div style="font-family: Georgia, serif; font-size: 32px; color: var(--cp-gold); opacity: 0.2; line-height: 1;">⸻</div>
            <div class="text-caption text-medium-emphasis mt-2">Select a chapter</div>
          </div>
        </div>
      </div>

      <!-- Memory detail (right) -->
      <div class="flex-grow-1 d-flex flex-column" style="min-width: 0; overflow: hidden;">
        <template v-if="loadingDetail">
          <div class="pa-5">
            <div class="entry-skeleton mb-3" style="height: 120px;" />
            <div class="entry-skeleton mb-2" style="height: 60px;" />
            <div class="entry-skeleton" style="height: 180px;" />
          </div>
        </template>
        <template v-else-if="selectedEntry && detailMemory">
          <!-- Header -->
          <div class="px-5 pt-5 pb-0" style="flex-shrink: 0;">
            <div class="cp-detail-header mb-3">
              <div class="cp-detail-rule" />
              <h2 class="cp-detail-title">{{ detailMemory.summary || 'Entry' }}</h2>
              <div class="d-flex align-center ga-3 mt-2 flex-wrap">
                <span class="text-caption" style="color: var(--cp-muted);">{{ detailMemory.source_type }} · {{ detailMemory.source_machine }}</span>
                <span class="text-caption" style="color: var(--cp-muted);">{{ formatDate(detailMemory.created_at) }}</span>
                <v-chip v-if="detailMemory.category" size="x-small" variant="tonal" class="cp-topic-chip">{{ detailMemory.category }}</v-chip>
              </div>
              <div class="cp-detail-rule mt-3" />
            </div>
          </div>
          <!-- Tab bar -->
          <div class="cp-detail-tabs px-5" style="flex-shrink: 0;">
            <button :class="['cp-tab', activeDetailTab === 'details' && 'cp-tab-active']" @click="activeDetailTab = 'details'">Distilled</button>
            <button :class="['cp-tab', activeDetailTab === 'origin' && 'cp-tab-active']" @click="onDetailTabChange('origin')">Origin</button>
          </div>
          <!-- Tab content -->
          <div style="flex-grow: 1; overflow-y: auto; min-height: 0;">
            <!-- Details -->
            <div v-show="activeDetailTab === 'details'" class="pa-5 pt-3">
              <div class="d-flex ga-4 mb-5">
                <div v-if="detailMemory.importance !== undefined">
                  <div class="text-caption" style="color: var(--cp-muted); margin-bottom: 4px;">Importance</div>
                  <div class="d-flex align-center ga-1">
                    <v-progress-linear :model-value="detailMemory.importance * 100" color="amber-darken-1" height="4" rounded style="width: 64px;" />
                    <span class="text-caption">{{ (detailMemory.importance * 100).toFixed(0) }}%</span>
                  </div>
                </div>
                <div v-if="detailMemory.quality_score !== undefined && detailMemory.quality_score !== null">
                  <div class="text-caption" style="color: var(--cp-muted); margin-bottom: 4px;">Quality</div>
                  <div class="d-flex align-center ga-1">
                    <v-progress-linear :model-value="detailMemory.quality_score * 100" color="teal" height="4" rounded style="width: 64px;" />
                    <span class="text-caption">{{ (detailMemory.quality_score * 100).toFixed(0) }}%</span>
                  </div>
                </div>
              </div>
              <div class="cp-content-block mb-5">
                <p class="cp-body-text">{{ detailMemory.content }}</p>
              </div>
              <div v-if="detailMemory.tags?.length" class="mb-4">
                <div class="cp-section-label mb-2">Tags</div>
                <div class="d-flex flex-wrap ga-1">
                  <v-chip v-for="tag in detailMemory.tags" :key="tag" size="x-small" variant="outlined" class="cp-topic-chip">{{ tag }}</v-chip>
                </div>
              </div>
              <div v-if="detailEntities?.length" class="mb-4">
                <div class="cp-section-label mb-2">Named in this entry</div>
                <div class="d-flex flex-wrap ga-1">
                  <v-chip v-for="ent in detailEntities" :key="ent.id" size="x-small" variant="tonal" color="secondary">{{ ent.canonical_name }}</v-chip>
                </div>
              </div>
            </div>

            <!-- Origin -->
            <div v-show="activeDetailTab === 'origin'" class="pa-4">
              <div v-if="originLoading" class="d-flex align-center justify-center pa-8">
                <v-progress-circular indeterminate size="20" color="amber-darken-2" />
                <span class="ml-3 text-caption" style="color: var(--cp-muted);">Tracing source…</span>
              </div>
              <div v-else-if="originError" class="text-center pa-6">
                <v-icon icon="mdi-alert-circle-outline" color="error" size="24" class="mb-2 d-block mx-auto" />
                <div class="text-caption text-medium-emphasis">{{ originError }}</div>
              </div>
              <template v-else-if="origin">
                <!-- Conversation -->
                <template v-if="origin.origin_kind === 'conversation' && origin.conversation">
                  <div class="cp-origin-header mb-3">
                    <div class="d-flex align-center ga-2 mb-1">
                      <v-icon icon="mdi-chat-processing-outline" size="13" style="color: var(--cp-gold); opacity: 0.7;" />
                      <span class="text-caption font-weight-medium" style="color: rgba(230,210,180,0.85);">{{ origin.conversation.title }}</span>
                    </div>
                    <div class="d-flex ga-3 text-caption" style="color: var(--cp-muted);">
                      <span v-if="origin.conversation.model">{{ origin.conversation.model }}</span>
                      <span v-if="origin.conversation.original_date">{{ formatOriginalDate(origin.conversation.original_date) }}</span>
                      <span>{{ origin.conversation.message_count }} messages</span>
                      <span style="color: rgba(200,169,110,0.7);">window {{ origin.conversation.window_index + 1 }}/{{ origin.conversation.total_windows }}</span>
                    </div>
                  </div>
                  <div class="cp-conversation-thread" ref="threadEl">
                    <template v-for="(msg, idx) in visibleMessages" :key="idx">
                      <details v-if="msg.role === 'tool'" class="cp-tool-details mb-1" :class="{ 'cp-win-highlight': isInWindow(msg._globalIdx, origin.conversation) }">
                        <summary class="text-caption" style="cursor: pointer; padding: 4px 8px; list-style: none; display: flex; align-items: center; gap: 4px; color: var(--cp-muted);">
                          <v-icon size="10">mdi-wrench-outline</v-icon> Tool output ({{ charCount(msg.content) }} chars)
                        </summary>
                        <pre class="cp-msg-content cp-tool-content mt-1">{{ msg.content }}</pre>
                      </details>
                      <div v-else class="cp-msg-bubble mb-2" :class="[`cp-role-${msg.role}`, { 'cp-win-highlight': isInWindow(msg._globalIdx, origin.conversation) }]" :data-msg-idx="msg._globalIdx">
                        <div class="cp-msg-label">
                          <span>{{ msg.role === 'user' ? 'You' : 'Assistant' }}</span>
                          <span v-if="msg.timestamp" class="ml-2" style="opacity: 0.35;">{{ formatMsgTime(msg.timestamp) }}</span>
                          <span v-if="isInWindow(msg._globalIdx, origin.conversation)" class="cp-distilled-chip ml-2">distilled</span>
                        </div>
                        <pre class="cp-msg-content">{{ msg.content }}</pre>
                      </div>
                    </template>
                    <div v-if="!showAllMessages && origin.conversation.messages.length > 30" class="text-center mt-3">
                      <button class="cp-show-all-btn" @click="showAllMessages = true">Show all {{ origin.conversation.messages.length }} messages</button>
                    </div>
                  </div>
                </template>

                <!-- Document chunks -->
                <template v-else-if="origin.origin_kind === 'document_chunk' && origin.document">
                  <div class="cp-origin-header mb-3">
                    <div class="d-flex align-center ga-2 mb-1">
                      <v-icon icon="mdi-file-document-outline" size="13" style="color: var(--cp-gold); opacity: 0.7;" />
                      <span class="text-caption font-weight-medium text-truncate" style="color: rgba(230,210,180,0.85);">{{ origin.document.file_path || origin.document.document_title || 'Document' }}</span>
                    </div>
                    <div class="text-caption" style="color: var(--cp-muted);">Chunk {{ origin.document.chunk_index + 1 }} of {{ origin.document.chunk_total }}</div>
                    <div v-if="origin.document.contextual_prefix" class="mt-2 text-caption" style="color: var(--cp-muted); font-style: italic;">{{ origin.document.contextual_prefix }}</div>
                  </div>
                  <div class="cp-chunk-pane">
                    <div v-for="chunk in origin.document.chunks" :key="chunk.chunk_index" class="cp-chunk-block" :class="{ 'cp-chunk-current': chunk.is_current }">
                      <div class="cp-chunk-label">§ {{ chunk.chunk_index + 1 }}<span v-if="chunk.is_current" class="cp-distilled-chip ml-2">this memory</span></div>
                      <pre class="cp-chunk-text">{{ chunk.content }}</pre>
                    </div>
                  </div>
                </template>

                <!-- Self / derived -->
                <template v-else-if="origin.origin_kind === 'self' || origin.origin_kind === 'derived'">
                  <div class="cp-origin-header mb-3">
                    <div class="d-flex align-center ga-2">
                      <v-icon :icon="origin.origin_kind === 'derived' ? 'mdi-merge' : 'mdi-file-outline'" size="13" style="color: var(--cp-gold); opacity: 0.7;" />
                      <span class="text-caption" style="color: rgba(230,210,180,0.85);">{{ origin.origin_kind === 'derived' ? 'Synthesised from multiple sources' : origin.source_type }}</span>
                    </div>
                  </div>
                  <pre class="cp-body-text" style="font-size: 13px; opacity: 0.8;">{{ origin.self_content }}</pre>
                </template>

                <!-- No source -->
                <template v-else>
                  <div class="text-center pa-8">
                    <div style="font-family: Georgia, serif; font-size: 32px; color: var(--cp-gold); opacity: 0.1; line-height: 1;">∅</div>
                    <div class="text-caption text-medium-emphasis mt-2">No source record for {{ origin.source_type }}</div>
                  </div>
                </template>
              </template>
              <div v-else class="text-center pa-8">
                <div style="font-family: Georgia, serif; font-size: 32px; color: var(--cp-gold); opacity: 0.1; line-height: 1;">↑</div>
                <div class="text-caption" style="color: var(--cp-muted); margin-top: 6px;">Click Origin to trace this entry</div>
              </div>
            </div>
          </div>
        </template>
        <div v-else class="d-flex align-center justify-center fill-height">
          <div class="text-center">
            <div style="font-family: Georgia, serif; font-size: 48px; color: var(--cp-gold); opacity: 0.12; line-height: 1;">§</div>
            <div class="text-caption text-medium-emphasis mt-2">Select an entry to read</div>
          </div>
        </div>
      </div>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onMounted } from 'vue'

interface Chapter {
  community_id: number
  title: string
  summary: string
  key_topics: string[]
  member_count: number
  memory_count: number
  hit_count?: number
  updated_at: string | null
  score: number | null
}

interface Entry {
  id: string
  summary: string | null
  content: string | null
  source_type: string | null
  source_machine: string | null
  tags: string[] | null
  category: string | null
  importance: number
  quality_score: number | null
  created_at: string | null
  rrf_score?: number
  community_id?: number
}

interface ConvMessage { role: string; content: string; timestamp?: number; _globalIdx: number }
interface OriginConv { title: string; model?: string; original_date?: string; message_count: number; messages: any[]; window_index: number; total_windows: number; window_start: number; window_end: number }
interface OriginDoc { file_path?: string; document_title?: string; chunk_index: number; chunk_total: number; contextual_prefix?: string; chunks: any[] }
interface Origin { memory_id: string; source_type: string; origin_kind: string; conversation?: OriginConv; document?: OriginDoc; self_content?: string; self_metadata?: Record<string, unknown> }

const chapters = ref<Chapter[]>([])
const entries = ref<Entry[]>([])
const selectedChapter = ref<Chapter | null>(null)
const selectedEntry = ref<Entry | null>(null)
const detailMemory = ref<Entry | null>(null)
const detailEntities = ref<any[]>([])
const loadingChapters = ref(false)
const loadingEntries = ref(false)
const loadingDetail = ref(false)
const searchQuery = ref('')
const searchMode = ref(false)
const searchHits = ref<Entry[]>([])
const activeDetailTab = ref('details')
const origin = ref<Origin | null>(null)
const originLoading = ref(false)
const originError = ref('')
const showAllMessages = ref(false)
const threadEl = ref<HTMLElement | null>(null)

let searchTimer: ReturnType<typeof setTimeout>

const ROMAN = [
  [1000, 'M'], [900, 'CM'], [500, 'D'], [400, 'CD'],
  [100, 'C'], [90, 'XC'], [50, 'L'], [40, 'XL'],
  [10, 'X'], [9, 'IX'], [5, 'V'], [4, 'IV'], [1, 'I'],
] as [number, string][]

function toRoman(n: number): string {
  let result = ''
  for (const [val, sym] of ROMAN) {
    while (n >= val) { result += sym; n -= val }
  }
  return result
}

function formatDate(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleDateString('en-GB', { year: 'numeric', month: 'short', day: 'numeric' })
}

async function fetchChapters(q = '') {
  loadingChapters.value = true
  try {
    const params = new URLSearchParams({ limit: '200' })
    if (q) params.set('q', q)
    const res = await fetch(`/api/commonplace?${params}`)
    chapters.value = await res.json()
  } finally {
    loadingChapters.value = false
  }
}

async function selectChapter(ch: Chapter) {
  selectedChapter.value = ch
  selectedEntry.value = null
  detailMemory.value = null
  detailEntities.value = []
  origin.value = null
  activeDetailTab.value = 'details'

  if (searchMode.value) {
    entries.value = searchHits.value.filter(h => h.community_id === ch.community_id)
    return
  }

  loadingEntries.value = true
  try {
    const res = await fetch(`/api/commonplace/${ch.community_id}/memories?limit=150`)
    entries.value = await res.json()
  } finally {
    loadingEntries.value = false
  }
}

async function selectEntry(entry: Entry) {
  selectedEntry.value = entry
  activeDetailTab.value = 'details'
  origin.value = null
  originError.value = ''
  showAllMessages.value = false
  loadingDetail.value = true
  try {
    const res = await fetch(`/api/memories/${entry.id}`)
    const data = await res.json()
    detailMemory.value = data.memory
    detailEntities.value = data.entities || []
  } finally {
    loadingDetail.value = false
  }
}

const visibleMessages = computed<ConvMessage[]>(() => {
  if (!origin.value?.conversation) return []
  const msgs = origin.value.conversation.messages.map((m: any, i: number) => ({ ...m, _globalIdx: i }))
  if (showAllMessages.value) return msgs
  const { window_start, window_end } = origin.value.conversation
  const PAD = 5
  return msgs.slice(Math.max(0, window_start - PAD), Math.min(msgs.length - 1, window_end + PAD) + 1)
})

function isInWindow(idx: number, conv: OriginConv) { return idx >= conv.window_start && idx <= conv.window_end }
function charCount(s: string) { return (s || '').length }
function formatOriginalDate(iso: string) { try { return new Date(iso).toLocaleDateString('en-GB', { year: 'numeric', month: 'short', day: 'numeric' }) } catch { return iso } }
function formatMsgTime(ts: number) { try { return new Date(ts * 1000).toLocaleDateString('en-GB', { month: 'short', day: 'numeric' }) } catch { return '' } }

async function loadOrigin() {
  if (!detailMemory.value || origin.value || originLoading.value) return
  originLoading.value = true
  originError.value = ''
  try {
    const res = await fetch(`/api/memories/${detailMemory.value.id}/origin`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    origin.value = await res.json()
    if (origin.value?.origin_kind === 'conversation') {
      await nextTick()
      const el = threadEl.value?.querySelector('[data-msg-idx]') as HTMLElement | null
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  } catch (e: unknown) {
    originError.value = e instanceof Error ? e.message : 'Failed to load origin'
  } finally {
    originLoading.value = false
  }
}

function onDetailTabChange(tab: string) {
  activeDetailTab.value = tab
  if (tab === 'origin') loadOrigin()
}

function onSearch() {
  clearTimeout(searchTimer)
  const q = (searchQuery.value || '').trim()
  if (!q) {
    if (searchMode.value) {
      searchMode.value = false
      searchHits.value = []
      selectedChapter.value = null
      selectedEntry.value = null
      entries.value = []
      detailMemory.value = null
      fetchChapters()
    }
    return
  }
  searchTimer = setTimeout(() => runSearch(q), 400)
}

async function runSearch(q: string) {
  loadingChapters.value = true
  try {
    const res = await fetch(`/api/commonplace/search?q=${encodeURIComponent(q)}&limit=80`)
    const data = await res.json()
    searchMode.value = true
    searchHits.value = data.hits || []
    chapters.value = data.chapters || []
    // Auto-select the best chapter
    if (chapters.value.length > 0) {
      await selectChapter(chapters.value[0])
    } else {
      selectedChapter.value = null
      selectedEntry.value = null
      entries.value = []
    }
  } finally {
    loadingChapters.value = false
  }
}

onMounted(() => fetchChapters())
</script>

<style scoped>
:root {
  --cp-gold: #c8a96e;
  --cp-muted: rgba(200, 169, 110, 0.45);
}

.chapters-panel {
  --cp-gold: #c8a96e;
  --cp-muted: rgba(200, 169, 110, 0.45);
  background: rgba(20, 15, 8, 0.4);
}

.entries-panel {
  --cp-gold: #c8a96e;
  --cp-muted: rgba(200, 169, 110, 0.45);
  background: rgba(18, 14, 8, 0.25);
}

.folio-heading {
  display: flex;
  align-items: center;
}

.chapter-numeral {
  font-family: Georgia, Palatino, serif;
  color: var(--cp-gold);
  opacity: 0.5;
  font-size: 11px;
}

.chapter-item {
  cursor: pointer;
  border-radius: 6px;
  margin: 2px 8px;
  transition: background 0.15s;
}
.chapter-item:hover {
  background: rgba(200, 169, 110, 0.07);
}
.chapter-active {
  background: rgba(200, 169, 110, 0.12) !important;
}
.chapter-active .chapter-numeral {
  opacity: 1;
}

.chapter-title {
  font-family: Georgia, Palatino, serif;
  color: rgba(230, 210, 180, 0.9);
  font-size: 13px;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.chapter-skeleton {
  height: 52px;
  border-radius: 6px;
  background: rgba(200, 169, 110, 0.04);
  animation: shimmer 1.6s infinite;
  background-size: 200% 100%;
  background-image: linear-gradient(90deg, rgba(200,169,110,0.04) 25%, rgba(200,169,110,0.08) 50%, rgba(200,169,110,0.04) 75%);
}

.folio-chapter-header {
  border-left: 2px solid var(--cp-gold);
  padding-left: 10px;
  opacity: 0.9;
}

.chapter-title-large {
  font-family: Georgia, Palatino, serif;
  font-size: 15px;
  font-weight: 600;
  color: rgba(230, 210, 180, 0.95);
  line-height: 1.35;
}

.chapter-summary {
  color: rgba(200, 180, 150, 0.55);
  font-size: 11px;
  line-height: 1.5;
  margin: 0;
  font-style: italic;
}

.cp-topic-chip {
  border-color: rgba(200, 169, 110, 0.25) !important;
  color: rgba(200, 169, 110, 0.75) !important;
  font-size: 10px !important;
}

.entry-skeleton {
  height: 62px;
  border-radius: 6px;
  background-image: linear-gradient(90deg, rgba(200,169,110,0.04) 25%, rgba(200,169,110,0.07) 50%, rgba(200,169,110,0.04) 75%);
  animation: shimmer 1.6s infinite;
  background-size: 200% 100%;
}

.entry-item {
  cursor: pointer;
  border-radius: 6px;
  margin: 2px 8px;
  border-left: 2px solid transparent;
  transition: background 0.15s, border-color 0.15s;
}
.entry-item:hover {
  background: rgba(200, 169, 110, 0.06);
  border-left-color: rgba(200, 169, 110, 0.25);
}
.entry-active {
  background: rgba(200, 169, 110, 0.1) !important;
  border-left-color: var(--cp-gold) !important;
}

.entry-summary {
  font-family: Georgia, Palatino, serif;
  font-size: 13px;
  color: rgba(230, 210, 185, 0.85);
  line-height: 1.4;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

/* Detail panel */
.cp-detail-rule {
  height: 1px;
  background: linear-gradient(to right, var(--cp-gold), transparent);
  opacity: 0.25;
}

.cp-detail-title {
  font-family: Georgia, Palatino, serif;
  font-size: 18px;
  font-weight: 600;
  color: rgba(235, 215, 185, 0.95);
  line-height: 1.4;
  margin: 8px 0 0;
}

.cp-section-label {
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cp-gold);
  opacity: 0.6;
  font-family: Georgia, serif;
  margin-bottom: 6px;
}

.cp-content-block {
  padding: 16px;
  border-radius: 6px;
  background: rgba(200, 169, 110, 0.04);
  border: 1px solid rgba(200, 169, 110, 0.1);
}

.cp-body-text {
  font-family: Georgia, Palatino, serif;
  font-size: 14px;
  line-height: 1.75;
  color: rgba(220, 200, 170, 0.85);
  white-space: pre-wrap;
  margin: 0;
}

.cp-search :deep(.v-field) {
  border-color: rgba(200, 169, 110, 0.2) !important;
}

.cp-hit-badge {
  font-size: 10px;
  font-family: Georgia, serif;
  color: rgba(200, 169, 110, 0.8);
  background: rgba(200, 169, 110, 0.1);
  border: 1px solid rgba(200, 169, 110, 0.2);
  border-radius: 3px;
  padding: 1px 5px;
  letter-spacing: 0.04em;
}

.cp-rrf-badge {
  font-size: 10px;
  font-family: Georgia, serif;
  color: rgba(200, 169, 110, 0.65);
  opacity: 0.8;
}

@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* ── Detail tabs ── */
.cp-detail-tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid rgba(200, 169, 110, 0.1);
  margin-bottom: 0;
}
.cp-tab {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 7px 14px;
  font-family: Georgia, Palatino, serif;
  font-size: 12px;
  letter-spacing: 0.04em;
  color: rgba(200, 169, 110, 0.4);
  cursor: pointer;
  margin-bottom: -1px;
  transition: color 0.15s, border-color 0.15s;
}
.cp-tab:hover { color: rgba(200, 169, 110, 0.7); }
.cp-tab-active {
  color: var(--cp-gold) !important;
  border-bottom-color: var(--cp-gold);
}

/* ── Origin panel ── */
.cp-origin-header {
  border-left: 2px solid rgba(200, 169, 110, 0.3);
  padding-left: 10px;
}
.cp-conversation-thread { display: flex; flex-direction: column; gap: 2px; }
.cp-msg-bubble {
  border-radius: 6px;
  padding: 8px 10px;
  background: rgba(200, 169, 110, 0.03);
  border: 1px solid rgba(200, 169, 110, 0.06);
}
.cp-role-user { background: rgba(200, 169, 110, 0.05); }
.cp-role-assistant { background: rgba(180, 160, 130, 0.03); }
.cp-win-highlight {
  border-color: rgba(200, 169, 110, 0.35) !important;
  background: rgba(200, 169, 110, 0.08) !important;
}
.cp-msg-label {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgba(200, 169, 110, 0.45);
  margin-bottom: 5px;
  display: flex;
  align-items: center;
}
.cp-msg-content {
  font-family: Georgia, Palatino, serif;
  font-size: 13px;
  line-height: 1.65;
  color: rgba(220, 200, 170, 0.8);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.cp-tool-content { font-family: monospace; font-size: 11px; opacity: 0.5; }
.cp-tool-details { border-radius: 4px; background: rgba(255,255,255,0.02); padding: 2px 6px; }
.cp-distilled-chip {
  display: inline-block;
  font-size: 9px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: rgba(200, 169, 110, 0.8);
  background: rgba(200, 169, 110, 0.12);
  border-radius: 3px;
  padding: 1px 5px;
  font-family: Georgia, serif;
}
.cp-show-all-btn {
  background: none;
  border: 1px solid rgba(200, 169, 110, 0.2);
  border-radius: 4px;
  padding: 4px 12px;
  font-family: Georgia, serif;
  font-size: 12px;
  color: rgba(200, 169, 110, 0.6);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.cp-show-all-btn:hover { border-color: rgba(200, 169, 110, 0.5); color: rgba(200, 169, 110, 0.9); }

/* ── Chunk reading pane ── */
.cp-chunk-pane { display: flex; flex-direction: column; gap: 12px; }
.cp-chunk-block {
  border-left: 2px solid rgba(200, 169, 110, 0.12);
  padding: 10px 12px;
  border-radius: 0 6px 6px 0;
  background: rgba(200, 169, 110, 0.02);
}
.cp-chunk-current {
  border-left-color: rgba(200, 169, 110, 0.55) !important;
  background: rgba(200, 169, 110, 0.06) !important;
}
.cp-chunk-label {
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: rgba(200, 169, 110, 0.4);
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  font-family: Georgia, serif;
}
.cp-chunk-text {
  font-family: Georgia, Palatino, serif;
  font-size: 13px;
  line-height: 1.7;
  color: rgba(220, 200, 170, 0.75);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>
