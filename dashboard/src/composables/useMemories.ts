import { ref } from 'vue'
import api from '@/api/client'
import type { Memory, Entity } from '@/types'
import { useStatsStore } from '@/stores/stats'

export function useMemories() {
  const memories = ref<Memory[]>([])
  const selectedMemory = ref<Memory | null>(null)
  const selectedEntities = ref<Entity[]>([])
  const loading = ref(false)
  const detailLoading = ref(false)
  const searchQuery = ref('')
  const categoryFilter = ref('')
  const machineFilter = ref('')
  const qualityFilter = ref(0)
  const categories = ref<string[]>([])
  const machines = ref<string[]>([])

  async function fetchMemories(params?: Record<string, string | number>) {
    loading.value = true
    try {
      const { data } = await api.get<Memory[]>('/api/memories', { params })
      memories.value = data
    } finally {
      loading.value = false
    }
  }

  async function fetchMemoryDetail(id: string) {
    detailLoading.value = true
    try {
      const { data } = await api.get<{ memory: Memory; entities: Entity[] }>(`/api/memories/${id}`)
      selectedMemory.value = data.memory
      selectedEntities.value = data.entities
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
    loading,
    detailLoading,
    searchQuery,
    categoryFilter,
    machineFilter,
    qualityFilter,
    categories,
    machines,
    fetchMemories,
    fetchMemoryDetail,
    updateMemory,
    deleteMemory,
    fetchCategories,
    fetchMachines,
  }
}
