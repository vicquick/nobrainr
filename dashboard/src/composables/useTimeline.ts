import { ref } from 'vue'
import api from '@/api/client'
import type { Memory } from '@/types'

const PAGE_SIZE = 50

export function useTimeline() {
  const memories = ref<Memory[]>([])
  const loading = ref(false)
  const loadingMore = ref(false)
  const offset = ref(0)
  const hasMore = ref(true)
  const categoryFilter = ref('')
  const machineFilter = ref('')

  async function fetchTimeline() {
    loading.value = true
    offset.value = 0
    try {
      const params: Record<string, string | number> = { limit: PAGE_SIZE, offset: 0 }
      if (categoryFilter.value) params.category = categoryFilter.value
      if (machineFilter.value) params.source_machine = machineFilter.value
      const { data } = await api.get<Memory[]>('/api/timeline', { params })
      memories.value = data
      hasMore.value = data.length >= PAGE_SIZE
      offset.value = data.length
    } finally {
      loading.value = false
    }
  }

  async function loadMore() {
    if (!hasMore.value || loadingMore.value) return
    loadingMore.value = true
    try {
      const params: Record<string, string | number> = { limit: PAGE_SIZE, offset: offset.value }
      if (categoryFilter.value) params.category = categoryFilter.value
      if (machineFilter.value) params.source_machine = machineFilter.value
      const { data } = await api.get<Memory[]>('/api/timeline', { params })
      memories.value.push(...data)
      hasMore.value = data.length >= PAGE_SIZE
      offset.value += data.length
    } finally {
      loadingMore.value = false
    }
  }

  return {
    memories,
    loading,
    loadingMore,
    offset,
    hasMore,
    categoryFilter,
    machineFilter,
    fetchTimeline,
    loadMore,
  }
}
