import { ref } from 'vue'
import { defineStore } from 'pinia'
import api from '@/api/client'
import type { Stats } from '@/types'

export const useStatsStore = defineStore('stats', () => {
  const stats = ref<Stats | null>(null)
  const loading = ref(false)

  // The store fetches once when it is created, and several views then call
  // fetchStats() again in onMounted — every page load asked /api/stats twice
  // (measured 2026-11-10). Callers now share the request that is already in
  // flight, and data younger than FRESH_MS is not fetched again unless forced.
  const FRESH_MS = 15_000
  let geholtUm = 0
  let laufend: Promise<void> | null = null

  function fetchStats({ force = false } = {}): Promise<void> {
    if (laufend) return laufend
    if (!force && stats.value && Date.now() - geholtUm < FRESH_MS) return Promise.resolve()
    loading.value = true
    laufend = api
      .get<Stats>('/api/stats')
      .then(({ data }) => {
        stats.value = data
        geholtUm = Date.now()
      })
      .finally(() => {
        loading.value = false
        laufend = null
      })
    return laufend
  }

  fetchStats()

  return { stats, loading, fetchStats }
})
