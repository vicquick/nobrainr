<template>
  <v-container fluid style="max-width: 1200px;">
    <!-- Header row with refresh indicator -->
    <div class="d-flex align-center mb-4">
      <v-icon icon="mdi-pulse" size="20" class="mr-2 text-medium-emphasis" />
      <span class="text-subtitle-1 font-weight-bold">Live Pulse</span>
      <v-spacer />
      <div class="d-flex align-center ga-2">
        <span class="text-caption text-medium-emphasis">refreshes in {{ countdown }}s</span>
        <div class="live-dot" :class="{ active: pulsing }" />
      </div>
    </div>

    <template v-if="!stats">
      <div class="skeleton-block mb-4" style="height: 160px;" />
      <div class="skeleton-block mb-4" style="height: 120px;" />
      <div class="skeleton-block" style="height: 200px;" />
    </template>

    <template v-else>
      <!-- Extraction Progress -->
      <v-card class="mb-4 pulse-card">
        <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
          <v-icon icon="mdi-lightning-bolt" size="20" class="mr-2 text-medium-emphasis" />
          <span class="text-subtitle-1 font-weight-bold">Extraction</span>
          <v-spacer />
          <v-chip
            v-if="rate !== null"
            size="small"
            variant="tonal"
            :color="rate > 0 ? 'success' : 'default'"
            class="font-weight-medium"
          >
            <v-icon icon="mdi-speedometer" size="12" class="mr-1" />
            {{ rate > 0 ? `${rate}/min` : 'idle' }}
          </v-chip>
        </div>

        <v-card-text class="pa-4">
          <!-- Big numbers row -->
          <div class="d-flex ga-3 mb-5 flex-wrap">
            <div class="stat-box flex-grow-1 text-center">
              <div class="text-h4 font-weight-bold text-primary">{{ stats.total_memories.toLocaleString() }}</div>
              <div class="text-caption text-medium-emphasis mt-1">Total Memories</div>
            </div>
            <div class="stat-box flex-grow-1 text-center">
              <div class="text-h4 font-weight-bold text-secondary">{{ stats.total_entities.toLocaleString() }}</div>
              <div class="text-caption text-medium-emphasis mt-1">Entities</div>
            </div>
            <div class="stat-box flex-grow-1 text-center">
              <div class="text-h4 font-weight-bold text-success">{{ stats.total_relations.toLocaleString() }}</div>
              <div class="text-caption text-medium-emphasis mt-1">Relations</div>
            </div>
            <div class="stat-box flex-grow-1 text-center">
              <div class="text-h4 font-weight-bold" :class="extractionPct >= 95 ? 'text-success' : 'text-warning'">
                {{ extractionPct }}%
              </div>
              <div class="text-caption text-medium-emphasis mt-1">Extracted</div>
            </div>
          </div>

          <!-- Extraction progress bar -->
          <div class="mb-3">
            <div class="d-flex align-center justify-space-between mb-2">
              <span class="text-body-2 text-medium-emphasis">
                {{ stats.extraction_done.toLocaleString() }} done
                <span class="ml-2 text-caption">/ {{ (stats.extraction_done + stats.extraction_pending).toLocaleString() }} total</span>
              </span>
              <span class="text-body-2 font-weight-bold text-medium-emphasis">
                {{ stats.extraction_pending.toLocaleString() }} pending
              </span>
            </div>
            <v-progress-linear
              :model-value="extractionPct"
              :color="extractionPct >= 95 ? 'success' : 'warning'"
              height="12"
              rounded
              bg-color="surface-bright"
              class="mb-2"
            />
          </div>

          <!-- Rate / ETA row -->
          <div v-if="rate !== null" class="d-flex ga-4 flex-wrap">
            <div class="meta-chip">
              <v-icon icon="mdi-trending-up" size="14" class="mr-1" />
              <span>{{ deltaLastPoll > 0 ? `+${deltaLastPoll}` : deltaLastPoll }} this poll</span>
            </div>
            <div v-if="rate > 0 && stats.extraction_pending > 0" class="meta-chip">
              <v-icon icon="mdi-timer-outline" size="14" class="mr-1" />
              <span>ETA {{ eta }}</span>
            </div>
            <div v-if="stats.extraction_pending === 0" class="meta-chip text-success">
              <v-icon icon="mdi-check-circle-outline" size="14" class="mr-1" />
              <span>Backlog clear</span>
            </div>
          </div>
        </v-card-text>
      </v-card>

      <!-- Sources + Categories row -->
      <v-row>
        <v-col cols="12" md="4">
          <v-card class="pulse-card h-100">
            <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <v-icon icon="mdi-import" size="18" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-2 font-weight-bold">Sources</span>
            </div>
            <v-card-text class="pa-3">
              <div
                v-for="src in topSources"
                :key="src.source_type"
                class="d-flex align-center mb-2"
              >
                <span class="text-caption source-label">{{ src.source_type }}</span>
                <v-progress-linear
                  :model-value="(src.cnt / stats.total_memories) * 100"
                  color="primary"
                  height="6"
                  rounded
                  bg-color="surface-bright"
                  class="flex-grow-1 mx-2"
                />
                <span class="text-caption font-weight-medium tabular" style="min-width: 48px; text-align: right;">
                  {{ src.cnt.toLocaleString() }}
                </span>
              </div>
            </v-card-text>
          </v-card>
        </v-col>

        <v-col cols="12" md="4">
          <v-card class="pulse-card h-100">
            <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <v-icon icon="mdi-tag-multiple-outline" size="18" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-2 font-weight-bold">Categories</span>
            </div>
            <v-card-text class="pa-3">
              <div
                v-for="cat in topCategories"
                :key="cat.category"
                class="d-flex align-center mb-2"
              >
                <span class="text-caption source-label">{{ cat.category }}</span>
                <v-progress-linear
                  :model-value="(cat.cnt / stats.total_memories) * 100"
                  color="secondary"
                  height="6"
                  rounded
                  bg-color="surface-bright"
                  class="flex-grow-1 mx-2"
                />
                <span class="text-caption font-weight-medium tabular" style="min-width: 48px; text-align: right;">
                  {{ cat.cnt.toLocaleString() }}
                </span>
              </div>
            </v-card-text>
          </v-card>
        </v-col>

        <v-col cols="12" md="4">
          <v-card class="pulse-card h-100">
            <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <v-icon icon="mdi-server-outline" size="18" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-2 font-weight-bold">Machines</span>
            </div>
            <v-card-text class="pa-3">
              <div
                v-for="m in topMachines"
                :key="m.source_machine"
                class="d-flex align-center mb-2"
              >
                <span class="text-caption source-label">{{ m.source_machine }}</span>
                <v-progress-linear
                  :model-value="(m.cnt / stats.total_memories) * 100"
                  color="info"
                  height="6"
                  rounded
                  bg-color="surface-bright"
                  class="flex-grow-1 mx-2"
                />
                <span class="text-caption font-weight-medium tabular" style="min-width: 48px; text-align: right;">
                  {{ m.cnt.toLocaleString() }}
                </span>
              </div>
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>

      <!-- Delta history sparkline (text) -->
      <v-card class="pulse-card mt-4" v-if="history.length > 1">
        <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
          <v-icon icon="mdi-chart-line" size="18" class="mr-2 text-medium-emphasis" />
          <span class="text-subtitle-2 font-weight-bold">Extraction History</span>
          <span class="text-caption text-medium-emphasis ml-2">(last {{ history.length }} polls, every 10s)</span>
        </div>
        <v-card-text class="pa-4">
          <div class="d-flex align-end ga-1" style="height: 48px;">
            <div
              v-for="(h, i) in history"
              :key="i"
              class="history-bar"
              :style="{
                height: maxDelta > 0 ? `${Math.max(4, (h.delta / maxDelta) * 48)}px` : '4px',
                opacity: 0.4 + (i / history.length) * 0.6,
              }"
              :title="`+${h.delta} extracted`"
            />
          </div>
          <div class="text-caption text-medium-emphasis mt-2">
            avg {{ avgRate }}/min over last {{ history.length }} polls
          </div>
        </v-card-text>
      </v-card>
    </template>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import api from '@/api/client'
import type { Stats } from '@/types'

const stats = ref<Stats | null>(null)
const pulsing = ref(false)
const countdown = ref(10)

interface HistoryPoint { ts: number; done: number; delta: number }
const history = ref<HistoryPoint[]>([])
const lastDone = ref<number | null>(null)

const POLL_INTERVAL = 10_000

let pollTimer: ReturnType<typeof setInterval> | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null

async function fetchStats() {
  try {
    const { data } = await api.get<Stats>('/api/stats')
    const prevDone = lastDone.value
    const delta = prevDone !== null ? data.extraction_done - prevDone : 0
    lastDone.value = data.extraction_done
    history.value = [
      ...history.value.slice(-29),
      { ts: Date.now(), done: data.extraction_done, delta: Math.max(0, delta) },
    ]
    stats.value = data
    pulsing.value = true
    setTimeout(() => { pulsing.value = false }, 600)
  } catch {
    // silent
  }
}

const extractionPct = computed(() => {
  if (!stats.value) return 0
  const total = stats.value.extraction_done + stats.value.extraction_pending
  return total > 0 ? Math.round((stats.value.extraction_done / total) * 100) : 100
})

const deltaLastPoll = computed(() => {
  if (history.value.length < 2) return 0
  return history.value[history.value.length - 1].delta
})

const rate = computed((): number | null => {
  if (history.value.length < 2) return null
  const recent = history.value.slice(-6)
  const totalDelta = recent.reduce((s, h) => s + h.delta, 0)
  const minutes = (recent.length * POLL_INTERVAL) / 60000
  return Math.round(totalDelta / minutes)
})

const avgRate = computed(() => {
  if (history.value.length < 2) return 0
  const total = history.value.reduce((s, h) => s + h.delta, 0)
  const minutes = (history.value.length * POLL_INTERVAL) / 60000
  return Math.round(total / minutes)
})

const eta = computed(() => {
  if (!stats.value || !rate.value || rate.value <= 0) return '—'
  const minsLeft = Math.ceil(stats.value.extraction_pending / rate.value)
  if (minsLeft < 60) return `~${minsLeft}m`
  const h = Math.floor(minsLeft / 60)
  const m = minsLeft % 60
  return `~${h}h ${m}m`
})

const maxDelta = computed(() => Math.max(...history.value.map(h => h.delta), 1))

const topSources = computed(() => {
  if (!stats.value) return []
  return [...stats.value.by_source].sort((a, b) => b.cnt - a.cnt).slice(0, 8)
})

const topCategories = computed(() => {
  if (!stats.value) return []
  return stats.value.by_category
    .filter(c => !c.category.startsWith('_') && c.cnt > 5)
    .sort((a, b) => b.cnt - a.cnt)
    .slice(0, 8)
})

const topMachines = computed(() => {
  if (!stats.value) return []
  return [...stats.value.by_machine].sort((a, b) => b.cnt - a.cnt).slice(0, 8)
})

onMounted(() => {
  fetchStats()
  pollTimer = setInterval(fetchStats, POLL_INTERVAL)
  countdownTimer = setInterval(() => {
    countdown.value = countdown.value <= 1 ? 10 : countdown.value - 1
  }, 1000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (countdownTimer) clearInterval(countdownTimer)
})
</script>

<style scoped>
.pulse-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
}
.stat-box {
  padding: 12px 16px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.02);
  min-width: 100px;
}
.source-label {
  min-width: 100px;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}
.meta-chip {
  display: flex;
  align-items: center;
  font-size: 0.75rem;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  background: rgba(255, 255, 255, 0.03);
  border-radius: 8px;
  padding: 4px 10px;
  border: 1px solid rgba(255, 255, 255, 0.06);
}
.tabular {
  font-variant-numeric: tabular-nums;
}
.history-bar {
  width: 10px;
  background: rgb(var(--v-theme-primary));
  border-radius: 3px 3px 0 0;
  flex-shrink: 0;
  transition: height 300ms ease;
}
.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgba(var(--v-theme-on-surface), 0.2);
  transition: background 300ms ease;
}
.live-dot.active {
  background: rgb(var(--v-theme-success));
  box-shadow: 0 0 6px rgb(var(--v-theme-success));
}
.skeleton-block {
  background: linear-gradient(90deg, rgb(var(--v-theme-surface)) 25%, rgba(255,255,255,0.03) 50%, rgb(var(--v-theme-surface)) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 12px;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
</style>
