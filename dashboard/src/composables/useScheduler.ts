import { ref } from 'vue'
import api from '@/api/client'
import type { SchedulerStatus, AgentEvent, FeedbackStats, SystemHealth } from '@/types'

export function useScheduler() {
  const status = ref<SchedulerStatus | null>(null)
  const events = ref<AgentEvent[]>([])
  const feedbackStats = ref<FeedbackStats | null>(null)
  const health = ref<SystemHealth | null>(null)
  const loading = ref(true)

  const actionLoading = ref(false)

  async function pauseScheduler() {
    actionLoading.value = true
    try {
      await api.post('/api/scheduler/pause')
      await fetchScheduler()
    } finally {
      actionLoading.value = false
    }
  }

  async function resumeScheduler() {
    actionLoading.value = true
    try {
      await api.post('/api/scheduler/resume')
      await fetchScheduler()
    } finally {
      actionLoading.value = false
    }
  }

  async function fetchScheduler(opts: { silent?: boolean } = {}) {
    // Only show the loading skeleton on the very first fetch (when we
    // have no data yet) or when the caller explicitly asks. Subsequent
    // refreshes (SSE events, manual refresh) swap the data silently so
    // the page doesn't flash blank between updates.
    const isInitial = status.value === null && events.value.length === 0
    if (!opts.silent && isInitial) loading.value = true
    try {
      const { data } = await api.get<{
        scheduler_running: boolean
        scheduler_enabled: boolean
        jobs: Array<{
          name: string
          interval_hours: number
          type: string
          last_run: string | null
          run_count: number
        }>
        feedback: FeedbackStats
        recent_events: AgentEvent[]
        health: SystemHealth
      }>('/api/scheduler')
      status.value = {
        running: data.scheduler_running,
        tasks: (data.jobs || []).map(job => ({
          name: job.name,
          interval_hours: job.interval_hours,
          last_run: job.last_run,
          next_run: null,
          run_count: job.run_count,
          type: job.type,
        })),
      }
      events.value = data.recent_events
      feedbackStats.value = data.feedback
      health.value = data.health || null
    } finally {
      loading.value = false
    }
  }

  return {
    status,
    events,
    feedbackStats,
    health,
    loading,
    actionLoading,
    fetchScheduler,
    pauseScheduler,
    resumeScheduler,
  }
}
