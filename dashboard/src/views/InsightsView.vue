<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-4">
      <v-icon icon="mdi-lightbulb-on-outline" size="22" class="mr-2" />
      <h2 class="text-h5 font-weight-bold">Insights</h2>
      <span class="text-caption text-medium-emphasis ml-3">
        Synthesized lessons from your knowledge graph
      </span>
    </div>

    <div class="d-flex ga-3 mb-4 align-center">
      <v-btn-toggle v-model="since" mandatory density="comfortable" color="primary">
        <v-btn value="24h">24h</v-btn>
        <v-btn value="7d">7d</v-btn>
        <v-btn value="30d">30d</v-btn>
      </v-btn-toggle>
      <v-slider
        v-model="minConfidence"
        :min="0" :max="1" :step="0.05"
        label="Min confidence"
        thumb-label
        hide-details
        density="compact"
        style="max-width: 280px"
      />
      <span class="text-caption text-medium-emphasis ml-auto">
        {{ insights.length }} insights
      </span>
    </div>

    <v-card v-if="todayInsight" class="mb-4 today-card" elevation="2">
      <v-card-text class="pa-5">
        <div class="text-overline text-medium-emphasis mb-2">Insight of the day</div>
        <div class="text-body-1" style="font-family: Georgia, serif; line-height: 1.6">
          {{ (todayInsight.summary || todayInsight.content || '').slice(0, 600) }}
        </div>
        <div class="d-flex align-center ga-3 mt-3 text-caption text-medium-emphasis">
          <span v-if="todayInsight.created_at">
            {{ formatDate(todayInsight.created_at) }}
          </span>
          <v-chip v-if="todayInsight.quality_score" size="x-small" variant="tonal">
            quality {{ Math.round(todayInsight.quality_score * 100) }}%
          </v-chip>
          <v-chip v-if="todayInsight.confidence" size="x-small" variant="tonal" color="primary">
            confidence {{ Math.round(todayInsight.confidence * 100) }}%
          </v-chip>
        </div>
      </v-card-text>
    </v-card>

    <div v-if="loading" class="text-center pa-8">
      <v-progress-circular indeterminate size="32" />
    </div>
    <v-card v-else-if="!insights.length" class="pa-8 text-center text-medium-emphasis">
      No insights yet for the selected window.
    </v-card>
    <div v-else class="d-flex flex-column ga-3">
      <v-card v-for="ins in insights" :key="ins.id" elevation="0" class="insight-card">
        <v-card-text>
          <div class="text-body-2" style="line-height: 1.55">
            {{ (ins.summary || ins.content || '').slice(0, 500) }}
          </div>
          <div class="d-flex align-center ga-3 mt-2 text-caption text-medium-emphasis">
            <span>{{ formatDate(ins.created_at) }}</span>
            <v-chip v-if="ins.confidence" size="x-small" variant="tonal" color="primary">
              {{ Math.round(ins.confidence * 100) }}%
            </v-chip>
            <v-chip v-if="ins.quality_score" size="x-small" variant="tonal">
              q {{ Math.round(ins.quality_score * 100) }}%
            </v-chip>
            <span v-for="t in (ins.tags || []).slice(0, 3)" :key="t" class="text-caption text-medium-emphasis">
              · {{ t }}
            </span>
          </div>
        </v-card-text>
      </v-card>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'

interface Insight {
  id: string
  content?: string
  summary?: string
  created_at?: string
  confidence?: number
  quality_score?: number
  tags?: string[]
}

const since = ref('7d')
const minConfidence = ref(0.5)
const insights = ref<Insight[]>([])
const todayInsight = ref<Insight | null>(null)
const loading = ref(false)

async function loadInsights() {
  loading.value = true
  try {
    const r = await fetch(
      `/api/insights?since=${since.value}&min_confidence=${minConfidence.value}&limit=50`,
    )
    insights.value = await r.json()
  } finally {
    loading.value = false
  }
}

async function loadTodayInsight() {
  const r = await fetch('/api/insights/today')
  const d = await r.json()
  todayInsight.value = d.insight
}

function formatDate(s: string) {
  if (!s) return ''
  return new Date(s).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

watch([since, minConfidence], loadInsights)
onMounted(() => {
  loadInsights()
  loadTodayInsight()
})
</script>

<style scoped>
.today-card {
  background: linear-gradient(135deg, rgba(255, 200, 100, 0.08), rgba(255, 200, 100, 0.02));
  border-left: 3px solid rgba(255, 200, 100, 0.6);
}
.insight-card {
  border: 1px solid rgba(128, 128, 128, 0.15);
  transition: border-color 150ms;
}
.insight-card:hover {
  border-color: rgba(128, 128, 128, 0.3);
}
</style>
