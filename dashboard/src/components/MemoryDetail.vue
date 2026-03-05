<template>
  <v-card variant="outlined" class="fill-height">
    <v-card-title class="d-flex align-center pa-4">
      <span class="text-h6">{{ editing ? 'Edit Memory' : (memory.summary || 'Memory Detail') }}</span>
      <v-spacer />
      <v-btn
        :icon="editing ? 'mdi-close' : 'mdi-pencil'"
        variant="text"
        size="small"
        @click="editing = !editing"
      />
      <v-btn icon="mdi-delete" variant="text" size="small" color="error" @click="showDeleteDialog = true" />
    </v-card-title>

    <v-divider />

    <v-card-text class="pa-4" style="overflow-y: auto;">
      <!-- Edit Mode -->
      <template v-if="editing">
        <v-text-field
          v-model="editForm.summary"
          label="Summary"
          variant="outlined"
          density="compact"
          class="mb-3"
        />
        <v-textarea
          v-model="editForm.content"
          label="Content"
          variant="outlined"
          density="compact"
          rows="6"
          class="mb-3"
        />
        <v-text-field
          v-model="editForm.category"
          label="Category"
          variant="outlined"
          density="compact"
          class="mb-3"
        />
        <v-text-field
          v-model="editForm.tagsStr"
          label="Tags (comma-separated)"
          variant="outlined"
          density="compact"
          class="mb-3"
        />
        <v-btn color="primary" variant="flat" @click="handleSave" :loading="saving">
          Save
        </v-btn>
      </template>

      <!-- View Mode -->
      <template v-else>
        <!-- Metadata Grid -->
        <v-row dense class="mb-4">
          <v-col cols="6" sm="4">
            <div class="text-caption text-medium-emphasis">Category</div>
            <v-chip v-if="memory.category" size="small" variant="tonal" color="primary">
              {{ memory.category }}
            </v-chip>
            <span v-else class="text-body-2 text-medium-emphasis">--</span>
          </v-col>
          <v-col cols="6" sm="4">
            <div class="text-caption text-medium-emphasis">Source</div>
            <div class="text-body-2">{{ memory.source || '--' }}</div>
          </v-col>
          <v-col cols="6" sm="4">
            <div class="text-caption text-medium-emphasis">Machine</div>
            <div class="text-body-2">{{ memory.source_machine || '--' }}</div>
          </v-col>
          <v-col cols="6" sm="4">
            <div class="text-caption text-medium-emphasis">Importance</div>
            <v-progress-linear
              :model-value="memory.importance * 100"
              color="warning"
              height="6"
              rounded
            />
            <span class="text-caption">{{ (memory.importance * 100).toFixed(0) }}%</span>
          </v-col>
          <v-col cols="6" sm="4">
            <div class="text-caption text-medium-emphasis">Stability</div>
            <v-progress-linear
              :model-value="memory.stability * 100"
              color="success"
              height="6"
              rounded
            />
            <span class="text-caption">{{ (memory.stability * 100).toFixed(0) }}%</span>
          </v-col>
          <v-col cols="6" sm="4">
            <div class="text-caption text-medium-emphasis">Access Count</div>
            <div class="text-body-2">{{ memory.access_count }}</div>
          </v-col>
        </v-row>

        <!-- Content -->
        <div class="text-caption text-medium-emphasis mb-1">Content</div>
        <pre class="content-block pa-3 mb-4 rounded">{{ memory.content }}</pre>

        <!-- Tags -->
        <div v-if="memory.tags.length" class="mb-4">
          <div class="text-caption text-medium-emphasis mb-1">Tags</div>
          <div class="d-flex ga-1 flex-wrap">
            <v-chip v-for="tag in memory.tags" :key="tag" size="small" variant="outlined">
              {{ tag }}
            </v-chip>
          </div>
        </div>

        <!-- Entities -->
        <div v-if="entities && entities.length" class="mb-4">
          <div class="text-caption text-medium-emphasis mb-1">Entities</div>
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
        <v-divider class="mb-3" />
        <div class="d-flex ga-4 text-caption text-medium-emphasis">
          <span>Created: {{ formatDate(memory.created_at) }}</span>
          <span>Updated: {{ formatDate(memory.updated_at) }}</span>
        </div>
      </template>
    </v-card-text>

    <!-- Delete Dialog -->
    <v-dialog v-model="showDeleteDialog" max-width="400">
      <v-card>
        <v-card-title>Delete Memory?</v-card-title>
        <v-card-text>This action cannot be undone.</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="showDeleteDialog = false">Cancel</v-btn>
          <v-btn color="error" variant="flat" @click="handleDelete">Delete</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>

<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
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
.content-block {
  background: rgba(255, 255, 255, 0.05);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: monospace;
  font-size: 0.85rem;
  max-height: 400px;
  overflow-y: auto;
}
</style>
