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
  // True while full-text matches are on screen and the full search (embedding,
  // fusion, reranker) is still running; its answer replaces them.
  const refining = ref(false)

  // Two things used to go wrong here (measured 2026-11-10):
  // - the view's onMounted sets the filters from the URL (which fires the
  //   filter watcher) and then searches itself: two identical requests.
  //   Callers with the same parameters now share the request in flight.
  // - a slow answer for an older query could arrive after a newer one and
  //   overwrite its results. Only the most recent request may write.
  let laufend: { key: string; promise: Promise<void> } | null = null
  let folge = 0

  function fetchMemories(params?: Record<string, string | number>): Promise<void> {
    const p = params || {}
    const key = JSON.stringify(p)
    if (laufend && laufend.key === key) return laufend.promise
    loading.value = true
    lastParams.value = p
    const nr = ++folge
    refining.value = false
    let offen = true
    if (p.q) {
      // A text search takes 1-3s, up to 11s on a busy GPU. The text index
      // answers the same question in well under a second: show that list at
      // once, let the full answer replace it. A backend without the preview
      // route answers 404 and nothing changes.
      api
        .get<Memory[]>('/api/search/preview', { params: { ...p, limit: PAGE_SIZE } })
        .then(({ data }) => {
          if (nr !== folge || !offen || !Array.isArray(data) || data.length === 0) return
          memories.value = data
          hasMore.value = false
          loading.value = false
          refining.value = true
        })
        .catch(() => {})
    }
    const promise: Promise<void> = api
      .get<Memory[]>('/api/memories', { params: { ...p, limit: PAGE_SIZE, offset: 0 } })
      .then(({ data }) => {
        if (nr !== folge) return
        memories.value = data
        hasMore.value = data.length === PAGE_SIZE
      })
      .finally(() => {
        offen = false
        if (nr === folge) {
          loading.value = false
          refining.value = false
        }
        if (laufend && laufend.promise === promise) laufend = null
      })
    laufend = { key, promise }
    return promise
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

  // The facet row shows the most used tags (and "more" expands it). The full
  // list is ~1.4 MB of mostly single-use routing tags and rendering it as
  // chips is thousands of DOM nodes — ask for the head of the list only.
  // (Older backends ignore `limit` and still answer with everything.)
  const TAG_LIST_LIMIT = 300

  async function fetchTags() {
    const { data } = await api.get<{ tag: string; cnt: number }[]>('/api/tags', {
      params: { limit: TAG_LIST_LIMIT },
    })
    tags.value = data.slice(0, TAG_LIST_LIMIT)
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
    refining,
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
