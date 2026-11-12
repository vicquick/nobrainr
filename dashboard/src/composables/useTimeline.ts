import { ref } from 'vue'
import api from '@/api/client'
import type { Memory } from '@/types'

const PAGE_SIZE = 50

export function useTimeline() {
  const memories = ref<Memory[]>([])
  const loading = ref(true)
  const loadingMore = ref(false)
  const offset = ref(0)
  const hasMore = ref(true)
  const categoryFilter = ref('')
  const machineFilter = ref('')

  // Silent refreshes arrive in bursts: the SSE stream, the visibility change
  // and the post-store timer each ask for one, and the page fetched the same
  // list two or three times within a second (measured 2026-11-10). A silent
  // refresh is skipped while a fetch is in flight or when the list was
  // loaded less than SILENT_MIN_GAP_MS ago. A non-silent fetch (first load,
  // filter change) always runs.
  const SILENT_MIN_GAP_MS = 4_000
  let geholtUm = 0
  let laeuft = false

  let laufendKey = ''
  let laufendVersprechen: Promise<void> | null = null

  function fetchTimeline({ silent = false } = {}): Promise<void> {
    if (silent && (laeuft || Date.now() - geholtUm < SILENT_MIN_GAP_MS)) return Promise.resolve()
    const params: Record<string, string | number> = { limit: PAGE_SIZE, offset: 0, include_queue: 1 }
    if (categoryFilter.value) params.category = categoryFilter.value
    if (machineFilter.value) params.source_machine = machineFilter.value
    // A second caller with the same filters joins the request in flight
    // (mount + filter watcher fired together).
    const key = JSON.stringify(params)
    if (laufendVersprechen && laufendKey === key) return laufendVersprechen
    laufendKey = key
    laufendVersprechen = holeTimeline(params, silent).finally(() => {
      if (laufendKey === key) laufendVersprechen = null
    })
    return laufendVersprechen
  }

  async function holeTimeline(params: Record<string, string | number>, silent: boolean) {
    if (!silent) loading.value = true
    laeuft = true
    offset.value = 0
    try {
      const { data } = await api.get<Memory[]>('/api/timeline', { params })
      memories.value = data
      const storedCount = data.filter(m => !m.queue_status).length
      hasMore.value = storedCount >= PAGE_SIZE
      offset.value = storedCount
      geholtUm = Date.now()
    } finally {
      loading.value = false
      laeuft = false
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
