<template>
  <v-container fluid style="max-width: 1200px;">
    <template v-if="loading">
      <div class="skeleton-block mb-4" style="height: 300px;" />
      <div class="skeleton-block" style="height: 200px;" />
    </template>

    <template v-else>
      <v-row>
        <!-- Scheduler Status -->
        <v-col cols="12" lg="7">
          <v-card class="mb-4 scheduler-card">
            <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <v-icon icon="mdi-calendar-clock" size="20" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-1 font-weight-bold">Scheduler</span>
              <v-spacer />
              <v-chip
                :color="status?.running ? 'success' : 'error'"
                size="small"
                variant="tonal"
                class="font-weight-medium"
              >
                <v-icon :icon="status?.running ? 'mdi-circle' : 'mdi-circle-outline'" size="8" class="mr-1" />
                {{ status?.running ? 'Running' : 'Stopped' }}
              </v-chip>
            </div>

            <v-table v-if="status?.tasks.length" class="scheduler-table">
              <thead>
                <tr>
                  <th class="text-left">Task</th>
                  <th class="text-left">Interval</th>
                  <th class="text-left">Last Run</th>
                  <th class="text-center">Runs</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="task in status.tasks" :key="task.name" class="task-row">
                  <td>
                    <span class="font-weight-medium">{{ task.name }}</span>
                    <v-chip
                      v-if="task.type"
                      size="x-small"
                      variant="tonal"
                      :color="task.type === 'llm' ? 'warning' : 'info'"
                      class="ml-2 font-weight-bold"
                      style="font-size: 10px;"
                    >
                      {{ task.type }}
                    </v-chip>
                  </td>
                  <td class="text-medium-emphasis">{{ task.interval_hours }}h</td>
                  <td class="text-caption text-medium-emphasis" style="font-variant-numeric: tabular-nums;">
                    {{ task.last_run ? formatRelative(task.last_run) : '--' }}
                  </td>
                  <td class="text-center">
                    <v-chip size="x-small" variant="tonal" color="primary" class="font-weight-medium">
                      {{ task.run_count }}
                    </v-chip>
                  </td>
                </tr>
              </tbody>
            </v-table>

            <div v-else class="pa-6 text-center text-medium-emphasis">
              No scheduled tasks
            </div>
          </v-card>
        </v-col>

        <!-- Feedback Stats -->
        <v-col cols="12" lg="5">
          <v-card class="mb-4 scheduler-card">
            <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
              <v-icon icon="mdi-thumb-up-outline" size="20" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-1 font-weight-bold">Feedback</span>
            </div>

            <v-card-text v-if="feedbackStats" class="pa-4">
              <div class="d-flex ga-4 mb-4">
                <div class="text-center flex-grow-1 feedback-stat">
                  <div class="text-h4 font-weight-bold">{{ feedbackStats.total }}</div>
                  <div class="text-caption text-medium-emphasis mt-1">Total</div>
                </div>
                <div class="text-center flex-grow-1 feedback-stat">
                  <div class="text-h4 font-weight-bold text-success">{{ feedbackStats.positive }}</div>
                  <div class="text-caption text-medium-emphasis mt-1">Positive</div>
                </div>
                <div class="text-center flex-grow-1 feedback-stat">
                  <div class="text-h4 font-weight-bold text-error">{{ feedbackStats.negative }}</div>
                  <div class="text-caption text-medium-emphasis mt-1">Negative</div>
                </div>
              </div>
              <div>
                <div class="d-flex align-center justify-space-between mb-2">
                  <span class="text-caption text-medium-emphasis">Positive Rate</span>
                  <span class="text-body-2 font-weight-bold text-success">
                    {{ (feedbackStats.positive_rate * 100).toFixed(0) }}%
                  </span>
                </div>
                <v-progress-linear
                  :model-value="feedbackStats.positive_rate * 100"
                  color="success"
                  height="8"
                  rounded
                  bg-color="surface-bright"
                />
              </div>
            </v-card-text>

            <div v-else class="pa-6 text-center text-medium-emphasis">
              No feedback data
            </div>
          </v-card>
        </v-col>
      </v-row>

      <!-- Events Log -->
      <v-card class="scheduler-card">
        <div class="d-flex align-center pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
          <v-icon icon="mdi-history" size="20" class="mr-2 text-medium-emphasis" />
          <span class="text-subtitle-1 font-weight-bold">Recent Events</span>
          <v-chip v-if="events.length" size="x-small" variant="tonal" class="ml-2">{{ events.length }}</v-chip>
        </div>

        <v-table v-if="events.length" class="scheduler-table">
          <thead>
            <tr>
              <th class="text-left">Type</th>
              <th class="text-left">Source</th>
              <th class="text-left">Data</th>
              <th class="text-left">Time</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="evt in events" :key="evt.id" class="task-row">
              <td>
                <v-chip size="x-small" variant="tonal" color="info" class="font-weight-medium">
                  {{ evt.event_type }}
                </v-chip>
              </td>
              <td class="text-caption text-medium-emphasis">{{ evt.source || '--' }}</td>
              <td class="text-caption text-medium-emphasis" style="max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                {{ JSON.stringify(evt.event_data).slice(0, 100) }}
              </td>
              <td class="text-caption text-medium-emphasis" style="white-space: nowrap; font-variant-numeric: tabular-nums;">
                {{ formatRelative(evt.created_at) }}
              </td>
            </tr>
          </tbody>
        </v-table>

        <div v-else class="pa-6 text-center text-medium-emphasis">
          No recent events
        </div>
      </v-card>
    </template>
  </v-container>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useScheduler } from '@/composables/useScheduler'
import { useSSE } from '@/composables/useSSE'

const { status, events, feedbackStats, loading, fetchScheduler } = useScheduler()

function formatRelative(iso: string) {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

useSSE((evt) => {
  if (['agent_event', 'feedback_added'].includes(evt.type)) {
    fetchScheduler()
  }
})

onMounted(() => {
  fetchScheduler()
})
</script>

<style scoped>
.scheduler-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
}
.scheduler-table {
  background: transparent !important;
}
.scheduler-table th {
  font-size: 0.75rem !important;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)) !important;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04) !important;
}
.task-row {
  transition: background 100ms ease;
}
.task-row:hover {
  background: rgba(255, 255, 255, 0.02);
}
.task-row td {
  border-bottom: 1px solid rgba(255, 255, 255, 0.02) !important;
}
.feedback-stat {
  padding: 12px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.02);
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
