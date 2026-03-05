<template>
  <v-container fluid>
    <template v-if="loading">
      <v-skeleton-loader type="card" class="mb-4" />
      <v-skeleton-loader type="table" />
    </template>

    <template v-else>
      <v-row>
        <!-- Scheduler Status -->
        <v-col cols="12" md="6">
          <v-card variant="outlined" class="mb-4">
            <v-card-title class="d-flex align-center">
              <v-icon icon="mdi-calendar-clock" class="mr-2" />
              Scheduler
              <v-spacer />
              <v-chip
                :color="status?.running ? 'success' : 'error'"
                size="small"
                variant="flat"
              >
                <v-icon :icon="status?.running ? 'mdi-circle' : 'mdi-circle-outline'" size="x-small" class="mr-1" />
                {{ status?.running ? 'Running' : 'Stopped' }}
              </v-chip>
            </v-card-title>

            <v-divider />

            <v-table v-if="status?.tasks.length" density="compact">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Interval</th>
                  <th>Last Run</th>
                  <th>Next Run</th>
                  <th>Runs</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="task in status.tasks" :key="task.name">
                  <td class="font-weight-medium">
                    {{ task.name }}
                    <v-chip v-if="task.type" size="x-small" variant="tonal" :color="task.type === 'llm' ? 'warning' : 'info'" class="ml-1">{{ task.type }}</v-chip>
                  </td>
                  <td>{{ task.interval_hours }}h</td>
                  <td class="text-caption">{{ task.last_run ? new Date(task.last_run).toLocaleString() : '--' }}</td>
                  <td class="text-caption">{{ task.next_run ? new Date(task.next_run).toLocaleString() : '--' }}</td>
                  <td>{{ task.run_count }}</td>
                </tr>
              </tbody>
            </v-table>

            <v-card-text v-else class="text-medium-emphasis">
              No scheduled tasks
            </v-card-text>
          </v-card>
        </v-col>

        <!-- Feedback Stats -->
        <v-col cols="12" md="6">
          <v-card variant="outlined" class="mb-4">
            <v-card-title>
              <v-icon icon="mdi-thumb-up-outline" class="mr-2" />
              Feedback
            </v-card-title>

            <v-divider />

            <v-card-text v-if="feedbackStats">
              <v-row dense>
                <v-col cols="4" class="text-center">
                  <div class="text-h5">{{ feedbackStats.total }}</div>
                  <div class="text-caption text-medium-emphasis">Total</div>
                </v-col>
                <v-col cols="4" class="text-center">
                  <div class="text-h5 text-success">{{ feedbackStats.positive }}</div>
                  <div class="text-caption text-medium-emphasis">Positive</div>
                </v-col>
                <v-col cols="4" class="text-center">
                  <div class="text-h5 text-error">{{ feedbackStats.negative }}</div>
                  <div class="text-caption text-medium-emphasis">Negative</div>
                </v-col>
              </v-row>
              <div class="mt-3">
                <div class="text-caption text-medium-emphasis mb-1">
                  Positive Rate: {{ (feedbackStats.positive_rate * 100).toFixed(0) }}%
                </div>
                <v-progress-linear
                  :model-value="feedbackStats.positive_rate * 100"
                  color="success"
                  height="8"
                  rounded
                />
              </div>
            </v-card-text>

            <v-card-text v-else class="text-medium-emphasis">
              No feedback data
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>

      <!-- Events Log -->
      <v-card variant="outlined">
        <v-card-title>
          <v-icon icon="mdi-history" class="mr-2" />
          Recent Events
        </v-card-title>

        <v-divider />

        <v-table v-if="events.length" density="compact">
          <thead>
            <tr>
              <th>Type</th>
              <th>Source</th>
              <th>Data</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="evt in events" :key="evt.id">
              <td>
                <v-chip size="x-small" variant="tonal" color="info">{{ evt.event_type }}</v-chip>
              </td>
              <td class="text-caption">{{ evt.source || '--' }}</td>
              <td class="text-caption" style="max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                {{ JSON.stringify(evt.event_data).slice(0, 100) }}
              </td>
              <td class="text-caption">{{ new Date(evt.created_at).toLocaleString() }}</td>
            </tr>
          </tbody>
        </v-table>

        <v-card-text v-else class="text-medium-emphasis">
          No recent events
        </v-card-text>
      </v-card>
    </template>
  </v-container>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useScheduler } from '@/composables/useScheduler'

const { status, events, feedbackStats, loading, fetchScheduler } = useScheduler()

onMounted(() => {
  fetchScheduler()
})
</script>
