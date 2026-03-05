import { ref } from 'vue'
import api from '@/api/client'
import type { SchedulerStatus, AgentEvent, FeedbackStats } from '@/types'

export function useScheduler() {
  const status = ref<SchedulerStatus | null>(null)
  const events = ref<AgentEvent[]>([])
  const feedbackStats = ref<FeedbackStats | null>(null)
  const loading = ref(false)

  async function fetchScheduler() {
    loading.value = true
    try {
      const { data } = await api.get<{
        scheduler_running: boolean
        scheduler_enabled: boolean
        maintenance_interval_hours: number
        feedback_interval_hours: number
        feedback: FeedbackStats
        recent_events: AgentEvent[]
      }>('/api/scheduler')
      status.value = {
        running: data.scheduler_running,
        tasks: [
          {
            name: 'maintenance',
            interval_hours: data.maintenance_interval_hours,
            last_run: null,
            next_run: null,
            run_count: 0,
          },
          {
            name: 'feedback_integration',
            interval_hours: data.feedback_interval_hours,
            last_run: null,
            next_run: null,
            run_count: 0,
          },
        ],
      }
      events.value = data.recent_events
      feedbackStats.value = data.feedback
    } finally {
      loading.value = false
    }
  }

  return {
    status,
    events,
    feedbackStats,
    loading,
    fetchScheduler,
  }
}
