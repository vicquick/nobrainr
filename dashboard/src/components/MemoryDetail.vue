<template>
  <v-card variant="flat" class="fill-height detail-card d-flex flex-column">
    <!-- Header -->
    <div class="d-flex align-center pa-4 pb-0" style="border-bottom: 1px solid rgba(255,255,255,0.06);">
      <div class="flex-grow-1 min-w-0 mr-2">
        <div class="text-h6 font-weight-bold" style="line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
          {{ editing ? 'Edit Memory' : (memory.summary || 'Memory Detail') }}
        </div>
        <div class="text-caption text-medium-emphasis mt-1">
          {{ memory.source_type || 'unknown' }} &middot; {{ memory.source_machine || 'unknown' }}
        </div>
      </div>
      <v-btn
        :icon="editing ? 'mdi-close' : 'mdi-pencil-outline'"
        variant="text"
        size="small"
        @click="editing = !editing"
      />
      <v-btn icon="mdi-delete-outline" variant="text" size="small" color="error" @click="showDeleteDialog = true" />
    </div>

    <!-- Tabs -->
    <v-tabs
      v-model="activeTab"
      density="compact"
      height="36"
      style="border-bottom: 1px solid rgba(255,255,255,0.06); flex-shrink: 0;"
      @update:model-value="onTabChange"
    >
      <v-tab value="details" class="text-caption text-none px-4">Details</v-tab>
      <v-tab value="origin" class="text-caption text-none px-4">
        <v-icon size="13" class="mr-1">mdi-source-branch</v-icon>
        Origin
      </v-tab>
    </v-tabs>

    <!-- Tab panels -->
    <div class="flex-grow-1" style="overflow-y: auto; min-height: 0;">
      <!-- ── DETAILS TAB ─────────────────────────────────────────── -->
      <div v-show="activeTab === 'details'" class="pa-4">
        <!-- Edit Mode -->
        <template v-if="editing">
          <div class="d-flex flex-column ga-3">
            <v-text-field v-model="editForm.summary" label="Summary" />
            <v-textarea v-model="editForm.content" label="Content" rows="6" />
            <v-text-field v-model="editForm.category" label="Category" />
            <v-text-field v-model="editForm.tagsStr" label="Tags (comma-separated)" />
            <v-btn color="primary" variant="flat" @click="handleSave" :loading="saving" class="align-self-start">
              Save Changes
            </v-btn>
          </div>
        </template>

        <!-- View Mode -->
        <template v-else>
          <div class="d-flex ga-4 mb-5">
            <div class="stat-block">
              <div class="text-caption text-medium-emphasis mb-1">Importance</div>
              <div class="d-flex align-center ga-2">
                <v-progress-linear :model-value="memory.importance * 100" color="warning" height="6" rounded style="width: 80px;" />
                <span class="text-caption font-weight-medium">{{ (memory.importance * 100).toFixed(0) }}%</span>
              </div>
            </div>
            <div class="stat-block">
              <div class="text-caption text-medium-emphasis mb-1">Stability</div>
              <div class="d-flex align-center ga-2">
                <v-progress-linear :model-value="memory.stability * 100" color="success" height="6" rounded style="width: 80px;" />
                <span class="text-caption font-weight-medium">{{ (memory.stability * 100).toFixed(0) }}%</span>
              </div>
            </div>
            <div v-if="memory.quality_score != null" class="stat-block">
              <div class="text-caption text-medium-emphasis mb-1">Quality</div>
              <div class="d-flex align-center ga-2">
                <v-progress-linear :model-value="(memory.quality_score ?? 0) * 100" :color="qualityColor" height="6" rounded style="width: 80px;" />
                <span class="text-caption font-weight-medium">{{ ((memory.quality_score ?? 0) * 100).toFixed(0) }}%</span>
              </div>
            </div>
            <div class="stat-block">
              <div class="text-caption text-medium-emphasis mb-1">Accessed</div>
              <div class="text-body-2 font-weight-medium">{{ memory.access_count }}&times;</div>
            </div>
          </div>

          <div v-if="memory.quality_specificity != null" class="d-flex ga-3 mb-5" style="opacity: 0.7;">
            <div class="text-caption"><span class="text-medium-emphasis">Specificity</span> {{ memory.quality_specificity }}/5</div>
            <div class="text-caption"><span class="text-medium-emphasis">Actionability</span> {{ memory.quality_actionability }}/5</div>
            <div class="text-caption"><span class="text-medium-emphasis">Self-contained</span> {{ memory.quality_self_containment }}/5</div>
          </div>

          <div v-if="memory.category" class="mb-4">
            <span class="cp-cat-lozenge">{{ memory.category }}</span>
          </div>

          <div class="mb-5">
            <div class="cp-detail-eyebrow">Content</div>
            <!-- v-html is safe here: useMarkdown.ts pipes the content
                 through marked → DOMPurify with a strict allowlist.
                 Plain-text paths are escapeHtml'd instead. -->
            <div class="cp-prose" v-html="renderedContent" />
          </div>

          <div v-if="memory.tags.length" class="mb-5">
            <div class="cp-detail-eyebrow">Tags</div>
            <div class="d-flex ga-1 flex-wrap">
              <span v-for="tag in memory.tags" :key="tag" class="cp-tag-pill">{{ tag }}</span>
            </div>
          </div>

          <div v-if="facts && facts.length" class="mb-5">
            <div class="text-caption text-medium-emphasis mb-2 text-uppercase" style="letter-spacing: 0.5px;">
              Facts <span class="text-caption" style="opacity: 0.5;">({{ facts.length }})</span>
            </div>
            <div class="d-flex flex-column ga-1">
              <div v-for="fact in facts" :key="fact.id" class="fact-item pa-2 rounded">
                <v-icon icon="mdi-lightning-bolt" size="14" color="amber" class="mr-1" style="vertical-align: text-top;" />
                <span class="text-body-2">{{ fact.content }}</span>
              </div>
            </div>
          </div>

          <div v-if="entities && entities.length" class="mb-5">
            <div class="text-caption text-medium-emphasis mb-2 text-uppercase" style="letter-spacing: 0.5px;">Entities</div>
            <div class="d-flex ga-1 flex-wrap">
              <EntityBadge v-for="e in entities" :key="e.id" :type="e.entity_type" :label="e.canonical_name" />
            </div>
          </div>

          <div style="border-top: 1px solid rgba(255,255,255,0.06);" class="pt-3">
            <div class="d-flex ga-4 text-caption text-medium-emphasis">
              <span>Created {{ formatDate(memory.created_at) }}</span>
              <span>Updated {{ formatDate(memory.updated_at) }}</span>
            </div>
          </div>
        </template>
      </div>

      <!-- ── ORIGIN TAB ─────────────────────────────────────────── -->
      <div v-show="activeTab === 'origin'" class="pa-4">
        <div v-if="originLoading" class="d-flex align-center justify-center pa-8">
          <v-progress-circular indeterminate size="24" color="primary" />
          <span class="ml-3 text-body-2 text-medium-emphasis">Loading source…</span>
        </div>

        <div v-else-if="originError" class="text-center pa-6">
          <v-icon icon="mdi-alert-circle-outline" color="error" size="28" class="mb-2 d-block mx-auto" />
          <div class="text-caption text-medium-emphasis">{{ originError }}</div>
        </div>

        <template v-else-if="origin">
          <!-- ── Conversation ── -->
          <template v-if="origin.origin_kind === 'conversation' && origin.conversation">
            <div class="origin-header mb-3">
              <div class="d-flex align-center ga-2 mb-1">
                <v-icon icon="mdi-chat-processing-outline" size="14" color="primary" />
                <span class="text-caption font-weight-medium">{{ origin.conversation.title }}</span>
              </div>
              <div class="d-flex ga-3 text-caption text-medium-emphasis">
                <span v-if="origin.conversation.model">{{ origin.conversation.model }}</span>
                <span v-if="origin.conversation.original_date">{{ formatOriginalDate(origin.conversation.original_date) }}</span>
                <span>{{ origin.conversation.message_count }} messages</span>
                <span style="color: rgba(255,191,0,0.7);">
                  Window {{ origin.conversation.window_index + 1 }} / {{ origin.conversation.total_windows }}
                  (msgs {{ origin.conversation.window_start + 1 }}–{{ origin.conversation.window_end + 1 }})
                </span>
              </div>
            </div>

            <div class="conversation-thread" ref="threadEl">
              <template v-for="(msg, idx) in visibleMessages" :key="idx">
                <!-- Tool messages collapsed by default -->
                <details
                  v-if="msg.role === 'tool'"
                  class="tool-msg-details mb-1"
                  :class="{ 'window-highlight': isInWindow(msg._globalIdx, origin.conversation) }"
                >
                  <summary class="text-caption text-medium-emphasis" style="cursor: pointer; padding: 4px 8px; list-style: none; display: flex; align-items: center; gap: 4px;">
                    <v-icon size="11">mdi-wrench-outline</v-icon>
                    Tool output ({{ charCount(msg.content) }} chars)
                  </summary>
                  <pre class="msg-content tool-content mt-1">{{ msg.content }}</pre>
                </details>

                <div
                  v-else
                  class="msg-bubble mb-2"
                  :class="[`role-${msg.role}`, { 'window-highlight': isInWindow(msg._globalIdx, origin.conversation) }]"
                  :data-msg-idx="msg._globalIdx"
                >
                  <div class="msg-role-label text-caption mb-1">
                    <span>{{ msg.role === 'user' ? 'You' : 'Assistant' }}</span>
                    <span v-if="msg.timestamp" class="ml-2" style="opacity: 0.4;">
                      {{ formatMsgTime(msg.timestamp) }}
                    </span>
                    <span
                      v-if="isInWindow(msg._globalIdx, origin.conversation)"
                      class="cp-this-memory-mark ml-2"
                    >distilled from here</span>
                  </div>
                  <pre class="msg-content">{{ msg.content }}</pre>
                </div>
              </template>

              <div v-if="!showAllMessages && origin.conversation.messages.length > MSG_PREVIEW_COUNT" class="text-center mt-3">
                <v-btn
                  size="small"
                  variant="tonal"
                  @click="showAllMessages = true"
                >
                  Show all {{ origin.conversation.messages.length }} messages
                </v-btn>
              </div>
            </div>
          </template>

          <!-- ── Document Chunks ── -->
          <template v-else-if="origin.origin_kind === 'document_chunk' && origin.document">
            <div class="origin-header mb-3">
              <div class="d-flex align-center ga-2 mb-1">
                <v-icon icon="mdi-file-document-outline" size="14" color="secondary" />
                <span class="text-caption font-weight-medium text-truncate">
                  {{ origin.document.file_path || origin.document.document_title || 'Document' }}
                </span>
              </div>
              <div class="text-caption text-medium-emphasis">
                Chunk {{ origin.document.chunk_index + 1 }} of {{ origin.document.chunk_total }}
              </div>
              <div v-if="origin.document.contextual_prefix" class="mt-2 pa-2 rounded origin-prefix">
                <v-icon icon="mdi-information-outline" size="12" class="mr-1" style="opacity: 0.5;" />
                <span class="text-caption" style="opacity: 0.65; font-style: italic;">{{ origin.document.contextual_prefix }}</span>
              </div>
            </div>

            <div class="chunk-reading-pane">
              <div
                v-for="chunk in origin.document.chunks"
                :key="chunk.chunk_index"
                class="chunk-block"
                :class="{ 'chunk-current': chunk.is_current }"
              >
                <div class="chunk-index-label text-caption">
                  <span>§ {{ chunk.chunk_index + 1 }}</span>
                  <span v-if="chunk.is_current" class="cp-this-memory-mark">this memory</span>
                </div>
                <pre class="chunk-content">{{ chunk.content }}</pre>
              </div>
            </div>
          </template>

          <!-- ── Self-contained / Derived ── -->
          <template v-else-if="origin.origin_kind === 'self' || origin.origin_kind === 'derived'">
            <div class="origin-header mb-3">
              <div class="d-flex align-center ga-2">
                <v-icon
                  :icon="origin.origin_kind === 'derived' ? 'mdi-merge' : sourceIcon(origin.source_type)"
                  size="14"
                  color="secondary"
                />
                <span class="text-caption font-weight-medium">
                  {{ origin.origin_kind === 'derived' ? 'Synthesised from multiple sources' : sourceLabel(origin.source_type) }}
                </span>
              </div>
            </div>
            <div v-if="origin.self_metadata && Object.keys(origin.self_metadata).length" class="mb-3">
              <div class="d-flex flex-wrap ga-2">
                <template v-for="(v, k) in origin.self_metadata" :key="k">
                  <span v-if="v" class="cp-meta-pill">
                    <em>{{ k }}:</em> {{ v }}
                  </span>
                </template>
              </div>
            </div>
            <div class="cp-prose" v-html="renderedSelfContent" />
          </template>

          <!-- ── No source ── -->
          <template v-else>
            <div class="text-center pa-8">
              <v-icon icon="mdi-ghost-outline" size="32" class="mb-2 d-block mx-auto" style="opacity: 0.2;" />
              <div class="text-caption text-medium-emphasis">No source record for {{ origin.source_type }}</div>
            </div>
          </template>
        </template>

        <div v-else class="text-center pa-8">
          <v-icon icon="mdi-source-branch" size="32" class="mb-2 d-block mx-auto" style="opacity: 0.12;" />
          <div class="text-caption text-medium-emphasis">Click to load origin</div>
        </div>
      </div>
    </div>

    <!-- Delete Dialog -->
    <v-dialog v-model="showDeleteDialog" max-width="380">
      <v-card rounded="xl" class="cp-delete-card">
        <v-card-text class="pa-5">
          <p class="cp-delete-eyebrow">Marginalia · erasure</p>
          <p class="cp-delete-title">— Strike this entry from the codex? —</p>
          <p class="cp-delete-tagline">
            <em>The line is rewritten with a tombstone hash so the same content
            cannot be re-ingested by the dedup classifier. The deletion is
            durable; recovery means re-importing the source.</em>
          </p>
        </v-card-text>
        <v-card-actions class="pa-4 pt-0">
          <v-spacer />
          <v-btn variant="text" @click="showDeleteDialog = false">Keep</v-btn>
          <v-btn color="error" variant="flat" @click="handleDelete">Strike</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>

<script setup lang="ts">
import { ref, reactive, watch, computed, nextTick } from 'vue'
import type { Memory, Entity, Fact } from '@/types'
import EntityBadge from './EntityBadge.vue'
import { renderMemoryMarkdown } from '@/composables/useMarkdown'

interface ConvMessage {
  role: string
  content: string
  timestamp?: number
  _globalIdx: number
}

interface OriginConversation {
  id: string
  title: string
  model: string | null
  original_date: string | null
  message_count: number
  messages: Omit<ConvMessage, '_globalIdx'>[]
  window_index: number
  total_windows: number
  window_start: number
  window_end: number
}

interface OriginDocument {
  file_path: string
  document_title: string
  document_id: string
  chunk_index: number
  chunk_total: number
  contextual_prefix: string | null
  chunks: {
    memory_id: string
    chunk_index: number
    content: string
    summary: string | null
    contextual_prefix: string | null
    is_current: boolean
  }[]
}

interface Origin {
  memory_id: string
  source_type: string
  origin_kind: 'conversation' | 'document_chunk' | 'self' | 'derived' | 'none'
  conversation?: OriginConversation
  document?: OriginDocument
  self_content?: string
  self_metadata?: Record<string, unknown>
}

const MSG_PREVIEW_COUNT = 30

const props = defineProps<{
  memory: Memory
  entities?: Entity[]
  facts?: Fact[]
}>()

const emit = defineEmits<{
  update: [body: Partial<Memory>]
  delete: []
}>()

const activeTab = ref('details')
const editing = ref(false)
const saving = ref(false)
const showDeleteDialog = ref(false)
const origin = ref<Origin | null>(null)
const originLoading = ref(false)
const originError = ref('')
const showAllMessages = ref(false)
const threadEl = ref<HTMLElement | null>(null)

// Render memory.content (and origin.self_content for derived/self
// memories) as sanitised HTML so markdown — common in chatgpt
// imports + claude exports + crawl chunks — actually formats.
// useMarkdown.ts handles escape + parse + DOMPurify allowlist.
const renderedContent = computed(() => renderMemoryMarkdown(props.memory.content))
const renderedSelfContent = computed(() =>
  renderMemoryMarkdown(origin.value?.self_content ?? ''),
)

const qualityColor = computed(() => {
  const q = props.memory.quality_score ?? 0
  if (q >= 0.8) return 'amber-darken-1'
  if (q >= 0.6) return 'light-green'
  if (q >= 0.4) return 'grey'
  return 'grey-darken-1'
})

const editForm = reactive({ content: '', summary: '', category: '', tagsStr: '' })

watch(() => props.memory, (m) => {
  editForm.content = m.content
  editForm.summary = m.summary || ''
  editForm.category = m.category || ''
  editForm.tagsStr = m.tags.join(', ')
  editing.value = false
  // Reset origin when memory changes
  activeTab.value = 'details'
  origin.value = null
  originError.value = ''
  showAllMessages.value = false
}, { immediate: true })

// Messages with injected _globalIdx for window highlighting
const visibleMessages = computed<ConvMessage[]>(() => {
  if (!origin.value?.conversation) return []
  const msgs = origin.value.conversation.messages.map((m, i) => ({ ...m, _globalIdx: i }))
  if (showAllMessages.value) return msgs
  // Show a window around the distilled region + some padding
  const { window_start, window_end } = origin.value.conversation
  const PAD = 5
  const from = Math.max(0, window_start - PAD)
  const to = Math.min(msgs.length - 1, window_end + PAD)
  const slice = msgs.slice(from, to + 1)
  // If we're showing a middle slice, prepend a summary header
  if (from > 0 || to < msgs.length - 1) {
    return slice
  }
  return slice
})

function isInWindow(globalIdx: number, conv: OriginConversation): boolean {
  return globalIdx >= conv.window_start && globalIdx <= conv.window_end
}

function charCount(s: string): number {
  return (s || '').length
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}

function formatOriginalDate(iso: string): string {
  try { return new Date(iso).toLocaleDateString('en-GB', { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

function formatMsgTime(ts: number): string {
  try { return new Date(ts * 1000).toLocaleDateString('en-GB', { month: 'short', day: 'numeric' }) }
  catch { return '' }
}

function sourceIcon(st: string): string {
  const map: Record<string, string> = {
    github: 'mdi-github',
    affine_memos: 'mdi-notebook-outline',
    sticky_notes: 'mdi-note-text-outline',
    manual: 'mdi-pencil-outline',
    session: 'mdi-console',
    claude: 'mdi-robot-outline',
    agent: 'mdi-robot-outline',
    synthesis: 'mdi-merge',
  }
  return map[st] || 'mdi-file-outline'
}

function sourceLabel(st: string): string {
  const map: Record<string, string> = {
    github: 'GitHub commit',
    affine_memos: 'Affine memo',
    sticky_notes: 'Sticky note',
    manual: 'Manual entry',
    session: 'Claude Code session',
    claude: 'Claude-generated',
    agent: 'Agent-generated',
  }
  return map[st] || st
}

async function loadOrigin() {
  if (origin.value || originLoading.value) return
  originLoading.value = true
  originError.value = ''
  try {
    const res = await fetch(`/api/memories/${props.memory.id}/origin`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    origin.value = await res.json()
    // After load, if it's a conversation, scroll to first window-highlighted message
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

function onTabChange(tab: string) {
  if (tab === 'origin') loadOrigin()
}

async function handleSave() {
  saving.value = true
  try {
    emit('update', {
      content: editForm.content,
      summary: editForm.summary || null,
      category: editForm.category || null,
      tags: editForm.tagsStr.split(',').map(t => t.trim()).filter(Boolean),
    })
    editing.value = false
  } finally {
    saving.value = false
  }
}

function handleDelete() {
  showDeleteDialog.value = false
  emit('delete')
}
</script>

<style scoped>
/* All MemoryDetail surfaces dressed in commonplace book aesthetic — gold
   on parchment, Georgia serif throughout, rules instead of cards. */
.detail-card {
  background: transparent !important;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: rgba(238, 224, 196, 0.94);
}
.detail-card :deep(.v-card-title),
.detail-card :deep(.v-tab) {
  font-family: Georgia, serif !important;
  letter-spacing: 0.04em;
}
.detail-card :deep(.v-tab--selected) { color: #c8a96e !important; }
.detail-card :deep(.v-card) { background: transparent !important; }

/* ── Details tab ── */
.content-block {
  background: rgba(200, 169, 110, 0.04);
  border: 1px solid rgba(200, 169, 110, 0.18);
  border-left: 2px solid #c8a96e;
  padding: 18px 22px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  font-size: 16px;
  line-height: 1.75;
  max-height: 540px;
  overflow-y: auto;
  color: rgba(238, 224, 196, 0.94);
}

/* .cp-prose moved to global tokens.css (also used by ThreadDetailView).
   See "Codex prose" section there for the rule body. */
.stat-block {
  min-width: 100px;
  font-family: Georgia, serif;
}
.stat-block :deep(.text-caption),
.stat-block :deep(.text-medium-emphasis) {
  font-style: italic;
  color: rgba(238, 224, 196, 0.55) !important;
  letter-spacing: 0.05em;
}
.fact-item {
  background: rgba(200, 169, 110, 0.04);
  border: 1px solid rgba(200, 169, 110, 0.18);
  border-left: 2px solid rgba(200, 169, 110, 0.55);
  font-family: Georgia, serif;
  font-size: 15px;
  line-height: 1.6;
  padding: 10px 14px;
}

/* ── Origin tab ── */
.origin-header {
  padding: 12px 16px;
  background: rgba(200, 169, 110, 0.05);
  border: 1px solid rgba(200, 169, 110, 0.18);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13.5px;
  color: rgba(238, 224, 196, 0.7);
}
.origin-prefix {
  background: rgba(200, 169, 110, 0.06);
  border: 1px solid rgba(200, 169, 110, 0.25);
  font-family: Georgia, serif;
  font-style: italic;
}

/* Conversation thread */
.conversation-thread {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.msg-bubble {
  padding: 10px 14px;
  border: 1px solid transparent;
  font-family: Georgia, serif;
}

.role-user {
  background: rgba(200, 169, 110, 0.05);
  border-color: rgba(200, 169, 110, 0.18);
  border-left: 2px solid rgba(200, 169, 110, 0.55);
}

.role-assistant {
  background: rgba(200, 169, 110, 0.02);
  border-color: rgba(200, 169, 110, 0.1);
}

.window-highlight.msg-bubble {
  border-color: #c8a96e !important;
  background: rgba(200, 169, 110, 0.1) !important;
  border-left: 2px solid #c8a96e !important;
}

.msg-role-label {
  display: flex;
  align-items: center;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: rgba(238, 224, 196, 0.55);
  margin-bottom: 4px;
}

.window-highlight .msg-role-label {
  color: #c8a96e;
}

.msg-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: Georgia, serif;
  font-size: 15px;
  line-height: 1.7;
  color: rgba(238, 224, 196, 0.92);
  margin: 0;
}

.tool-msg-details {
  border: 1px dotted rgba(200, 169, 110, 0.18);
  padding: 6px 10px;
  background: rgba(200, 169, 110, 0.02);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12.5px;
  color: rgba(238, 224, 196, 0.55);
}

.tool-content {
  max-height: 220px;
  overflow-y: auto;
  font-family: Georgia, serif;
  font-size: 13.5px;
  color: rgba(238, 224, 196, 0.65);
  font-style: italic;
}

.window-highlight.tool-msg-details {
  border-color: rgba(200, 169, 110, 0.45);
  background: rgba(200, 169, 110, 0.05);
}

/* Chunk reading pane */
.chunk-reading-pane {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.chunk-block {
  padding: 14px 18px;
  border-left: 2px solid rgba(200, 169, 110, 0.18);
  margin-bottom: 0;
  transition: all 200ms;
}

.chunk-current {
  border-left-color: #c8a96e !important;
  background: rgba(200, 169, 110, 0.05);
}

.chunk-index-label {
  display: flex;
  align-items: center;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12.5px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: rgba(238, 224, 196, 0.45);
  margin-bottom: 6px;
}

.chunk-current .chunk-index-label {
  color: #c8a96e;
}

.chunk-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  font-size: 16px;
  line-height: 1.75;
  color: rgba(238, 224, 196, 0.85);
  margin: 0;
}

.chunk-current .chunk-content {
  color: rgba(238, 224, 196, 0.96);
}

/* ── Codex polish utilities (replacing Vuetify chip defaults) ─────
   Each previously v-chip element gets a hand-drawn marginalia
   alternative so the read pane voice matches the rest of the
   dashboard. */

/* Section header replacing `text-caption text-medium-emphasis text-uppercase`
   sequences — italic small caps + 0.18em letter spacing, gold-soft. */
.cp-detail-eyebrow {
  font-style: italic;
  font-size: 12.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  margin: 0 0 8px;
}

/* Category lozenge — replaces v-chip color="primary" tonal */
.cp-cat-lozenge {
  display: inline-block;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  font-style: italic;
  font-size: 12.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--cp-gold);
  background: var(--cp-gold-trace);
  border: 1px solid var(--cp-gold-faint);
  padding: 3px 10px;
  border-radius: 2px;
}

/* Tag pill — replaces v-chip variant="outlined" */
.cp-tag-pill {
  display: inline-flex;
  align-items: center;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12.5px;
  letter-spacing: 0.04em;
  color: var(--cp-ink-mute);
  background: transparent;
  border: 1px solid var(--cp-rule);
  padding: 2px 8px;
  border-radius: 2px;
  transition:
    color var(--cp-dur-hover) var(--cp-ease),
    border-color var(--cp-dur-hover) var(--cp-ease);
}
.cp-tag-pill:hover { color: var(--cp-ink); border-color: var(--cp-gold-soft); }

/* "this memory" / "distilled from here" marker — replaces v-chip
   color="amber-darken-2" tonal. Reads as a marginal annotation. */
.cp-this-memory-mark {
  display: inline-block;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--cp-gold-bright);
  background: rgba(200, 169, 110, 0.10);
  border: 1px solid var(--cp-gold-faint);
  padding: 1px 7px;
  border-radius: 2px;
}

/* Origin metadata k:v pill — replaces v-chip x-small outlined */
.cp-meta-pill {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-family: Georgia, serif;
  font-size: 11.5px;
  letter-spacing: 0.04em;
  color: var(--cp-ink-mute);
  background: transparent;
  border: 1px solid var(--cp-rule);
  padding: 1px 8px;
  border-radius: 2px;
}
.cp-meta-pill em {
  font-style: italic;
  color: var(--cp-gold-soft);
}

/* Delete confirmation dialog — codex voice replaces "Delete Memory?".
   The v-dialog wrapper is left intact (Vuetify owns positioning +
   focus trap there); only the card chrome is rewritten. */
.cp-delete-card {
  background:
    radial-gradient(700px 400px at 50% 0%, rgba(196, 106, 106, 0.05), transparent 70%),
    linear-gradient(180deg, var(--cp-paper) 0%, var(--cp-paper-deep) 100%) !important;
  border: 1px solid var(--cp-rule) !important;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.cp-delete-eyebrow {
  margin: 0 0 8px;
  font-style: italic;
  font-size: 11.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
}
.cp-delete-title {
  margin: 0 0 8px;
  font-size: 19px;
  color: var(--cp-ink);
  font-variant: small-caps;
}
.cp-delete-tagline {
  margin: 0;
  font-size: 14.5px;
  color: var(--cp-ink-mute);
  line-height: 1.55;
  font-style: italic;
}
</style>
