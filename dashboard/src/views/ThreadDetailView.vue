<template>
  <v-container fluid class="pa-6" style="max-width: 920px">
    <div class="d-flex align-center mb-4">
      <v-btn icon="mdi-arrow-left" variant="text" size="small" @click="$router.back()" />
      <v-icon icon="mdi-forum-outline" size="22" class="mx-2" />
      <h2 class="text-h6 font-weight-bold flex-grow-1">{{ conv?.title || 'Thread' }}</h2>
      <v-chip v-if="conv?.source_type" size="small" variant="tonal">
        {{ conv.source_type }}
      </v-chip>
    </div>

    <div v-if="loading" class="text-center pa-8">
      <v-progress-circular indeterminate />
    </div>
    <v-card v-else-if="!conv" class="pa-8 text-center text-medium-emphasis">
      Thread not found.
    </v-card>
    <div v-else>
      <div class="text-caption text-medium-emphasis mb-3">
        {{ messages.length }} messages · imported {{ formatDate(conv.imported_at) }}
      </div>
      <div class="d-flex flex-column ga-3">
        <v-card
          v-for="(m, idx) in messages"
          :key="idx"
          :color="messageRole(m) === 'user' ? 'surface' : undefined"
          variant="outlined"
        >
          <v-card-text>
            <div class="d-flex align-center ga-2 mb-2">
              <v-icon
                :icon="messageRole(m) === 'user' ? 'mdi-account' : 'mdi-robot-outline'"
                size="16"
                class="text-medium-emphasis"
              />
              <span class="text-caption text-medium-emphasis text-uppercase">
                {{ messageRole(m) || 'message' }}
              </span>
            </div>
            <div class="message-content">{{ messageText(m) }}</div>
          </v-card-text>
        </v-card>
      </div>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const conv = ref<any>(null)
const loading = ref(true)

const messages = computed(() => {
  if (!conv.value?.messages) return []
  if (Array.isArray(conv.value.messages)) return conv.value.messages
  return []
})

function messageRole(m: any): string {
  if (typeof m !== 'object' || m === null) return ''
  return m.role || m.author?.role || ''
}

function messageText(m: any): string {
  if (typeof m !== 'object' || m === null) return String(m || '')
  if (typeof m.content === 'string') return m.content
  if (Array.isArray(m.content)) {
    return m.content.map((p: any) => typeof p === 'string' ? p : (p?.text || '')).join('\n')
  }
  if (m.parts && Array.isArray(m.parts)) {
    return m.parts.map((p: any) => typeof p === 'string' ? p : (p?.text || '')).join('\n')
  }
  return JSON.stringify(m).slice(0, 1000)
}

function formatDate(s?: string) {
  if (!s) return ''
  return new Date(s).toLocaleString()
}

async function load() {
  loading.value = true
  try {
    const r = await fetch(`/api/conversations/${route.params.id}`)
    if (r.ok) conv.value = await r.json()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.message-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, monospace;
  font-size: 13px;
  line-height: 1.55;
}
</style>
