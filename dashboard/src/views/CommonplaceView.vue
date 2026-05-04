<template>
  <v-container fluid class="fill-height pa-0">
    <div class="d-flex fill-height" style="width: 100%;">

      <!-- Chapter list (left) -->
      <div class="chapters-panel d-flex flex-column" style="width: 300px; min-width: 300px;">
        <div class="pa-3 pb-2">
          <div class="folio-heading mb-3">
            <v-icon icon="mdi-book-open-page-variant" size="16" class="mr-1" style="color: var(--cp-gold);" />
            <span style="font-family: Georgia, Palatino, serif; font-size: 13px; color: var(--cp-gold); letter-spacing: 0.08em; text-transform: uppercase;">Commonplace Book</span>
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
                    <span class="text-caption" style="color: var(--cp-muted);">{{ ch.memory_count.toLocaleString() }} entries</span>
                    <span v-if="ch.score !== null" class="text-caption" style="color: var(--cp-gold); opacity: 0.7;">
                      {{ (ch.score * 100).toFixed(0) }}% match
                    </span>
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
            <p class="text-caption chapter-summary">{{ selectedChapter.summary }}</p>
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
                  <span v-if="m.importance" class="text-caption" style="color: var(--cp-gold); opacity: 0.6;">
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
      <div class="flex-grow-1" style="overflow-y: auto; min-width: 0;">
        <template v-if="loadingDetail">
          <div class="pa-5">
            <div class="entry-skeleton mb-3" style="height: 120px;" />
            <div class="entry-skeleton mb-2" style="height: 60px;" />
            <div class="entry-skeleton" style="height: 180px;" />
          </div>
        </template>
        <template v-else-if="selectedEntry && detailMemory">
          <div class="pa-5">
            <div class="cp-detail-header mb-4">
              <div class="cp-detail-rule" />
              <h2 class="cp-detail-title">{{ detailMemory.summary || 'Entry' }}</h2>
              <div class="d-flex align-center ga-3 mt-2 flex-wrap">
                <span class="text-caption" style="color: var(--cp-muted);">{{ detailMemory.source_type }} · {{ detailMemory.source_machine }}</span>
                <span class="text-caption" style="color: var(--cp-muted);">{{ formatDate(detailMemory.created_at) }}</span>
                <v-chip v-if="detailMemory.category" size="x-small" variant="tonal" class="cp-topic-chip">{{ detailMemory.category }}</v-chip>
              </div>
              <div class="cp-detail-rule mt-3" />
            </div>

            <!-- Metrics -->
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

            <!-- Content -->
            <div class="cp-content-block mb-5">
              <div class="cp-section-label">Distilled</div>
              <p class="cp-body-text">{{ detailMemory.content }}</p>
            </div>

            <!-- Tags -->
            <div v-if="detailMemory.tags?.length" class="mb-4">
              <div class="cp-section-label mb-2">Tags</div>
              <div class="d-flex flex-wrap ga-1">
                <v-chip v-for="tag in detailMemory.tags" :key="tag" size="x-small" variant="outlined" class="cp-topic-chip">{{ tag }}</v-chip>
              </div>
            </div>

            <!-- Entities -->
            <div v-if="detailEntities?.length" class="mb-4">
              <div class="cp-section-label mb-2">Named in this entry</div>
              <div class="d-flex flex-wrap ga-1">
                <v-chip
                  v-for="ent in detailEntities"
                  :key="ent.id"
                  size="x-small"
                  variant="tonal"
                  color="secondary"
                >{{ ent.canonical_name }}</v-chip>
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
import { ref, onMounted } from 'vue'

interface Chapter {
  community_id: number
  title: string
  summary: string
  key_topics: string[]
  member_count: number
  memory_count: number
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
}

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

function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => fetchChapters(searchQuery.value), 350)
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
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
</style>
