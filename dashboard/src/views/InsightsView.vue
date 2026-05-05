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
      <v-card
        v-for="ins in insights"
        :key="ins.id"
        elevation="0"
        class="insight-card"
        @click="openInsight(ins)"
      >
        <v-card-text>
          <div class="text-body-2 insight-text" style="line-height: 1.55">
            {{ (ins.summary || ins.content || '').slice(0, 500) }}
          </div>
          <div class="d-flex align-center ga-3 mt-2 text-caption">
            <span class="insight-meta">{{ formatDate(ins.created_at) }}</span>
            <v-chip v-if="ins.confidence" size="x-small" variant="tonal" color="primary">
              {{ Math.round(ins.confidence * 100) }}%
            </v-chip>
            <v-chip v-if="ins.quality_score" size="x-small" variant="tonal">
              q {{ Math.round(ins.quality_score * 100) }}%
            </v-chip>
            <span v-for="t in (ins.tags || []).slice(0, 3)" :key="t" class="insight-meta">
              · {{ t }}
            </span>
            <v-icon icon="mdi-chevron-right" size="14" class="ml-auto insight-meta" />
          </div>
        </v-card-text>
      </v-card>
    </div>

    <v-dialog v-model="showDetail" max-width="780">
      <v-card v-if="selected" class="detail-card">
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-lightbulb-on" color="amber" size="20" />
          <span class="text-h6">Synthesis insight</span>
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" size="small" @click="showDetail = false" />
        </v-card-title>

        <v-card-text>
          <div class="full-content">{{ selected.content || selected.summary }}</div>

          <div class="d-flex flex-wrap ga-2 my-4">
            <v-chip v-if="selected.created_at" size="small" variant="tonal">
              <v-icon start size="14" icon="mdi-calendar-outline" />
              {{ formatDate(selected.created_at) }}
            </v-chip>
            <v-chip v-if="selected.confidence" size="small" variant="tonal" color="primary">
              confidence {{ Math.round(selected.confidence * 100) }}%
            </v-chip>
            <v-chip v-if="selected.quality_score" size="small" variant="tonal">
              quality {{ Math.round(selected.quality_score * 100) }}%
            </v-chip>
            <v-chip v-if="selected.category" size="small" variant="tonal">
              {{ selected.category }}
            </v-chip>
            <v-chip v-for="t in (selected.tags || [])" :key="t" size="small" variant="outlined">
              {{ t }}
            </v-chip>
          </div>

          <div v-if="sourceEntity" class="text-caption text-medium-emphasis mb-2">
            Synthesized from entity: <strong>{{ sourceEntity }}</strong>
          </div>

          <v-divider class="my-3" />
          <div class="text-overline mb-2 text-medium-emphasis">Related memories</div>
          <div v-if="loadingRelated" class="text-center pa-4">
            <v-progress-circular indeterminate size="24" />
          </div>
          <div v-else-if="!related.length" class="text-caption text-medium-emphasis">
            No closely related memories found.
          </div>
          <div v-else class="d-flex flex-column ga-2">
            <v-card
              v-for="m in related"
              :key="m.id"
              variant="outlined"
              class="related-card pa-3"
            >
              <div class="text-body-2">{{ (m.summary || m.content || '').slice(0, 240) }}</div>
              <div class="text-caption insight-meta mt-1">
                {{ m.source_type }} · {{ formatDate(m.created_at) }}
                <span v-if="m.similarity"> · sim {{ (m.similarity).toFixed(2) }}</span>
              </div>
            </v-card>
          </div>
        </v-card-text>
      </v-card>
    </v-dialog>
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

const showDetail = ref(false)
const selected = ref<any>(null)
const related = ref<any[]>([])
const loadingRelated = ref(false)
const sourceEntity = ref<string | null>(null)

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

async function openInsight(ins: any) {
  selected.value = ins
  showDetail.value = true
  related.value = []
  sourceEntity.value = null
  loadingRelated.value = true
  try {
    const detailR = await fetch(`/api/memories/${ins.id}`)
    if (detailR.ok) {
      const detail = await detailR.json()
      selected.value = { ...ins, ...detail.memory }
      const meta = detail.memory?.metadata || {}
      sourceEntity.value = meta.source_entity || null
    }
    const text = (ins.summary || ins.content || '').slice(0, 400)
    if (text) {
      const r = await fetch(
        `/api/memories?q=${encodeURIComponent(text)}&limit=6`,
      )
      if (r.ok) {
        const arr = await r.json()
        related.value = arr.filter((m: any) => m.id !== ins.id).slice(0, 5)
      }
    }
  } catch (e) {
    // ignore — detail just won't show extras
  } finally {
    loadingRelated.value = false
  }
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
  transition: border-color 150ms, transform 150ms;
  cursor: pointer;
}
.insight-card:hover {
  border-color: rgba(128, 128, 128, 0.4);
  transform: translateY(-1px);
}
.insight-text { color: rgba(255, 255, 255, 0.94); }
.insight-meta { color: rgba(255, 255, 255, 0.62); }
:deep(.v-theme--light) .insight-text { color: rgba(0, 0, 0, 0.92); }
:deep(.v-theme--light) .insight-meta { color: rgba(0, 0, 0, 0.55); }
.detail-card .full-content {
  white-space: pre-wrap;
  font-family: Georgia, Palatino, serif;
  font-size: 15px;
  line-height: 1.7;
  color: rgba(255, 255, 255, 0.94);
}
:deep(.v-theme--light) .detail-card .full-content { color: rgba(0, 0, 0, 0.92); }
.related-card {
  background: rgba(255, 255, 255, 0.02);
}
:deep(.v-theme--light) .related-card { background: rgba(0, 0, 0, 0.02); }
</style>
