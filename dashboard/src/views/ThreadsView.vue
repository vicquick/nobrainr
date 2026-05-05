<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-4">
      <v-icon icon="mdi-forum-outline" size="22" class="mr-2" />
      <h2 class="text-h5 font-weight-bold">Threads</h2>
      <span class="text-caption text-medium-emphasis ml-3">
        Raw conversations — your archived ChatGPT and Claude history
      </span>
    </div>

    <div class="d-flex ga-3 mb-4">
      <v-text-field
        v-model="query"
        prepend-inner-icon="mdi-magnify"
        placeholder="Search threads…"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        @keyup.enter="search"
      />
      <v-select
        v-model="sourceFilter"
        :items="[
          { title: 'All sources', value: null },
          { title: 'ChatGPT', value: 'chatgpt' },
          { title: 'Claude', value: 'claude_web' },
        ]"
        density="comfortable"
        variant="outlined"
        hide-details
        style="max-width: 200px"
        @update:model-value="search"
      />
      <v-btn @click="search" color="primary" :loading="loading">Search</v-btn>
    </div>

    <div v-if="loading" class="text-center pa-8">
      <v-progress-circular indeterminate size="32" />
    </div>
    <v-card v-else-if="!threads.length" class="pa-8 text-center text-medium-emphasis">
      {{ query ? 'No threads matched.' : 'Recent threads will appear here. Try searching.' }}
    </v-card>
    <div v-else class="d-flex flex-column ga-2">
      <v-card
        v-for="t in threads"
        :key="t.id"
        elevation="0"
        class="thread-card"
        @click="openThread(t.id)"
      >
        <v-card-text class="d-flex align-center ga-3">
          <v-icon
            :icon="t.source_type === 'chatgpt' ? 'mdi-open-ai' : 'mdi-robot-outline'"
            size="20"
            class="text-medium-emphasis"
          />
          <div class="flex-grow-1 min-w-0">
            <div class="text-body-1 font-weight-medium text-truncate">
              {{ t.title || '(untitled)' }}
            </div>
            <div class="text-caption text-medium-emphasis">
              {{ t.source_type }} · {{ t.message_count || 0 }} messages
              · {{ formatDate(t.imported_at) }}
            </div>
          </div>
          <v-icon icon="mdi-chevron-right" class="text-medium-emphasis" />
        </v-card-text>
      </v-card>
    </div>
  </v-container>
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
  return new Date(s).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

onMounted(search)
</script>

<style scoped>
.thread-card {
  border: 1px solid rgba(128, 128, 128, 0.15);
  cursor: pointer;
  transition: all 150ms;
}
.thread-card:hover {
  border-color: rgba(128, 128, 128, 0.4);
  transform: translateY(-1px);
}
</style>
