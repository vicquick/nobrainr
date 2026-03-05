import { ref } from 'vue'
import api from '@/api/client'
import type { GraphData, NodeDetail } from '@/types'

export function useGraph() {
  const graphData = ref<GraphData | null>(null)
  const selectedNode = ref<NodeDetail | null>(null)
  const loading = ref(false)
  const nodeLoading = ref(false)

  async function fetchGraph() {
    loading.value = true
    try {
      const { data } = await api.get<GraphData>('/api/graph')
      graphData.value = data
    } finally {
      loading.value = false
    }
  }

  async function fetchNodeDetail(entityId: string) {
    nodeLoading.value = true
    try {
      const { data } = await api.get<NodeDetail>(`/api/node/${entityId}`)
      selectedNode.value = data
    } finally {
      nodeLoading.value = false
    }
  }

  function clearSelection() {
    selectedNode.value = null
  }

  return {
    graphData,
    selectedNode,
    loading,
    nodeLoading,
    fetchGraph,
    fetchNodeDetail,
    clearSelection,
  }
}
