import { ref } from 'vue'
import api from '@/api/client'
import type { GraphData, NodeDetail } from '@/types'

export interface CommunityNode {
  data: {
    id: string
    community_id: number
    label: string
    x: number
    y: number
    size: number
    type: string
    color: string
    summary: string
    member_count: number
    top_entities: Array<{ name: string; type: string; mentions: number }>
    key_topics: string[]
  }
}

export interface CommunityEdge {
  data: { id: string; source: string; target: string; weight: number }
}

export interface CommunityGraphData {
  nodes: CommunityNode[]
  edges: CommunityEdge[]
}

export function useGraph() {
  const graphData = ref<GraphData | null>(null)
  const communityGraphData = ref<CommunityGraphData | null>(null)
  const selectedNode = ref<NodeDetail | null>(null)
  const loading = ref(false)
  const nodeLoading = ref(false)

  async function fetchGraph(communityFilter?: number) {
    loading.value = true
    const params: Record<string, string> = {}
    if (communityFilter !== undefined) {
      params.community = String(communityFilter)
    }
    const { data } = await api.get<GraphData>('/api/graph', { params })
    graphData.value = data
    // Caller sets loading = false after Sigma init
  }

  async function fetchCommunityGraph() {
    loading.value = true
    const { data } = await api.get<CommunityGraphData>('/api/graph/communities')
    communityGraphData.value = data
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
    communityGraphData,
    selectedNode,
    loading,
    nodeLoading,
    fetchGraph,
    fetchCommunityGraph,
    fetchNodeDetail,
    clearSelection,
  }
}
