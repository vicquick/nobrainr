<template>
  <v-card variant="flat" class="fill-height detail-card">
    <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.06);">
      <div class="flex-grow-1">
        <div class="text-h6 font-weight-bold" style="line-height: 1.3;">
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

    <div class="pa-4" style="overflow-y: auto;">
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
        <!-- Stats row -->
        <div class="d-flex ga-4 mb-5">
          <div class="stat-block">
            <div class="text-caption text-medium-emphasis mb-1">Importance</div>
            <div class="d-flex align-center ga-2">
              <v-progress-linear
                :model-value="memory.importance * 100"
                color="warning"
                height="6"
                rounded
                style="width: 80px;"
              />
              <span class="text-caption font-weight-medium">{{ (memory.importance * 100).toFixed(0) }}%</span>
            </div>
          </div>
          <div class="stat-block">
            <div class="text-caption text-medium-emphasis mb-1">Stability</div>
            <div class="d-flex align-center ga-2">
              <v-progress-linear
                :model-value="memory.stability * 100"
                color="success"
                height="6"
                rounded
                style="width: 80px;"
              />
              <span class="text-caption font-weight-medium">{{ (memory.stability * 100).toFixed(0) }}%</span>
            </div>
          </div>
          <div v-if="memory.quality_score != null" class="stat-block">
            <div class="text-caption text-medium-emphasis mb-1">Quality</div>
            <div class="d-flex align-center ga-2">
              <v-progress-linear
                :model-value="(memory.quality_score ?? 0) * 100"
                :color="qualityColor"
                height="6"
                rounded
                style="width: 80px;"
              />
              <span class="text-caption font-weight-medium">{{ ((memory.quality_score ?? 0) * 100).toFixed(0) }}%</span>
            </div>
          </div>
          <div class="stat-block">
            <div class="text-caption text-medium-emphasis mb-1">Accessed</div>
            <div class="text-body-2 font-weight-medium">{{ memory.access_count }}&times;</div>
          </div>
        </div>

        <!-- Quality breakdown -->
        <div v-if="memory.quality_specificity != null" class="d-flex ga-3 mb-5" style="opacity: 0.7;">
          <div class="text-caption">
            <span class="text-medium-emphasis">Specificity</span> {{ memory.quality_specificity }}/5
          </div>
          <div class="text-caption">
            <span class="text-medium-emphasis">Actionability</span> {{ memory.quality_actionability }}/5
          </div>
          <div class="text-caption">
            <span class="text-medium-emphasis">Self-contained</span> {{ memory.quality_self_containment }}/5
          </div>
        </div>

        <!-- Category -->
        <div v-if="memory.category" class="mb-4">
          <v-chip size="small" variant="tonal" color="primary" class="font-weight-medium">
            {{ memory.category }}
          </v-chip>
        </div>

        <!-- Content -->
        <div class="mb-5">
          <div class="text-caption text-medium-emphasis mb-2 text-uppercase" style="letter-spacing: 0.5px;">Content</div>
          <pre class="content-block">{{ memory.content }}</pre>
        </div>

        <!-- Tags -->
        <div v-if="memory.tags.length" class="mb-5">
          <div class="text-caption text-medium-emphasis mb-2 text-uppercase" style="letter-spacing: 0.5px;">Tags</div>
          <div class="d-flex ga-1 flex-wrap">
            <v-chip v-for="tag in memory.tags" :key="tag" size="small" variant="outlined">
              {{ tag }}
            </v-chip>
          </div>
        </div>

        <!-- Entities -->
        <div v-if="entities && entities.length" class="mb-5">
          <div class="text-caption text-medium-emphasis mb-2 text-uppercase" style="letter-spacing: 0.5px;">Entities</div>
          <div class="d-flex ga-1 flex-wrap">
            <EntityBadge
              v-for="e in entities"
              :key="e.id"
              :type="e.entity_type"
              :label="e.canonical_name"
            />
          </div>
        </div>

        <!-- Timestamps -->
        <div style="border-top: 1px solid rgba(255,255,255,0.06);" class="pt-3">
          <div class="d-flex ga-4 text-caption text-medium-emphasis">
            <span>Created {{ formatDate(memory.created_at) }}</span>
            <span>Updated {{ formatDate(memory.updated_at) }}</span>
          </div>
        </div>
      </template>
    </div>

    <!-- Delete Dialog -->
    <v-dialog v-model="showDeleteDialog" max-width="380">
      <v-card rounded="xl">
        <v-card-text class="pa-5">
          <div class="d-flex align-center mb-3">
            <v-icon icon="mdi-alert-circle-outline" color="error" size="24" class="mr-2" />
            <span class="text-h6 font-weight-bold">Delete Memory?</span>
          </div>
          <p class="text-body-2 text-medium-emphasis">This action cannot be undone.</p>
        </v-card-text>
        <v-card-actions class="pa-4 pt-0">
          <v-spacer />
          <v-btn variant="text" @click="showDeleteDialog = false">Cancel</v-btn>
          <v-btn color="error" variant="flat" @click="handleDelete">Delete</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>

<script setup lang="ts">
import { ref, reactive, watch, computed } from 'vue'
import type { Memory, Entity } from '@/types'
import EntityBadge from './EntityBadge.vue'

const props = defineProps<{
  memory: Memory
  entities?: Entity[]
}>()

const emit = defineEmits<{
  update: [body: Partial<Memory>]
  delete: []
}>()

const editing = ref(false)
const saving = ref(false)
const showDeleteDialog = ref(false)

const qualityColor = computed(() => {
  const q = props.memory.quality_score ?? 0
  if (q >= 0.8) return 'amber-darken-1'
  if (q >= 0.6) return 'light-green'
  if (q >= 0.4) return 'grey'
  return 'grey-darken-1'
})

const editForm = reactive({
  content: '',
  summary: '',
  category: '',
  tagsStr: '',
})

watch(
  () => props.memory,
  (m) => {
    editForm.content = m.content
    editForm.summary = m.summary || ''
    editForm.category = m.category || ''
    editForm.tagsStr = m.tags.join(', ')
    editing.value = false
  },
  { immediate: true },
)

function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}

async function handleSave() {
  saving.value = true
  try {
    emit('update', {
      content: editForm.content,
      summary: editForm.summary || null,
      category: editForm.category || null,
      tags: editForm.tagsStr.split(',').map((t) => t.trim()).filter(Boolean),
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
.detail-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
}
.content-block {
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid rgba(255, 255, 255, 0.04);
  border-radius: 12px;
  padding: 16px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.82rem;
  line-height: 1.6;
  max-height: 400px;
  overflow-y: auto;
  color: rgba(var(--v-theme-on-surface), var(--v-high-emphasis-opacity));
}
.stat-block {
  min-width: 100px;
}
</style>
