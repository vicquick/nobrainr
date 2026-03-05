import { ref } from 'vue'
import { defineStore } from 'pinia'
import api from '@/api/client'
import type { Stats } from '@/types'

export const useStatsStore = defineStore('stats', () => {
  const stats = ref<Stats | null>(null)
  const loading = ref(false)

  async function fetchStats() {
    loading.value = true
    try {
      const { data } = await api.get<Stats>('/api/stats')
      stats.value = data
    } finally {
      loading.value = false
    }
  }

  fetchStats()

  return { stats, loading, fetchStats }
})
