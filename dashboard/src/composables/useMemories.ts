import { ref } from 'vue'
import api from '@/api/client'
import type { Memory, Entity, Fact } from '@/types'
import { useStatsStore } from '@/stores/stats'

const PAGE_SIZE = 50

export function useMemories() {
  const memories = ref<Memory[]>([])
  const selectedMemory = ref<Memory | null>(null)
  const selectedEntities = ref<Entity[]>([])
  const selectedFacts = ref<Fact[]>([])
  const loading = ref(true)
  const loadingMore = ref(false)
  const hasMore = ref(false)
  const detailLoading = ref(false)
  const searchQuery = ref('')
  const categoryFilter = ref('')
  const machineFilter = ref('')
  const qualityFilter = ref(0)
  const tagsFilter = ref<Set<string>>(new Set())
  const categories = ref<string[]>([])
  const machines = ref<string[]>([])
  const tags = ref<{ tag: string; cnt: number }[]>([])
  const lastParams = ref<Record<string, string | number>>({})

  async function fetchMemories(params?: Record<string, string | number>) {
    loading.value = true
    lastParams.value = params || {}
    try {
      const { data } = await api.get<Memory[]>('/api/memories', {
        params: { ...params, limit: PAGE_SIZE, offset: 0 },
      })
      memories.value = data
      hasMore.value = data.length === PAGE_SIZE
    } finally {
      loading.value = false
    }
  }

  async function loadMore() {
    if (loadingMore.value || !hasMore.value) return
    loadingMore.value = true
    try {
      const { data } = await api.get<Memory[]>('/api/memories', {
        params: {
          ...lastParams.value,
          limit: PAGE_SIZE,
          offset: memories.value.length,
        },
      })
      memories.value = [...memories.value, ...data]
      hasMore.value = data.length === PAGE_SIZE
    } finally {
      loadingMore.value = false
    }
  }

  async function fetchMemoryDetail(id: string) {
    detailLoading.value = true
    try {
      const { data } = await api.get<{ memory: Memory; entities: Entity[] }>(`/api/memories/${id}`)
      selectedMemory.value = data.memory
      selectedEntities.value = data.entities
      // Fetch facts for this memory
      try {
        const factsRes = await api.get<{ facts: Fact[] }>(`/api/memories/${id}/facts`)
        selectedFacts.value = factsRes.data.facts
      } catch {
        selectedFacts.value = []
      }
    } finally {
      detailLoading.value = false
    }
  }

  async function updateMemory(id: string, body: Partial<Memory>) {
    const { data } = await api.post<Memory>(`/api/memories/${id}`, body)
    selectedMemory.value = data
    return data
  }

  async function deleteMemory(id: string) {
    await api.delete(`/api/memories/${id}`)
    memories.value = memories.value.filter((m) => m.id !== id)
    if (selectedMemory.value?.id === id) selectedMemory.value = null
  }

  async function fetchCategories() {
    const { data } = await api.get<string[]>('/api/categories')
    categories.value = data
  }

  async function fetchTags() {
    const { data } = await api.get<{ tag: string; cnt: number }[]>('/api/tags')
    tags.value = data
  }

  function fetchMachines() {
    const statsStore = useStatsStore()
    if (statsStore.stats) {
      machines.value = statsStore.stats.by_machine.map(m => m.source_machine)
    }
  }

  return {
    memories,
    selectedMemory,
    selectedEntities,
    selectedFacts,
    loading,
    loadingMore,
    hasMore,
    detailLoading,
    searchQuery,
    categoryFilter,
    machineFilter,
    qualityFilter,
    tagsFilter,
    categories,
    machines,
    tags,
    fetchMemories,
    loadMore,
    fetchMemoryDetail,
    updateMemory,
    deleteMemory,
    fetchCategories,
    fetchMachines,
    fetchTags,
  }
}
