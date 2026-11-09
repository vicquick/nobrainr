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

/** /api/graph?format=compact — parallel arrays; an edge is two node indexes and a label index. */
interface CompactGraph {
  format: 'compact-1'
  nodes: {
    id: string[]
    label: string[]
    type: string[]
    description: Array<string | null>
    mention_count: number[]
    x: number[]
    y: number[]
    community: number[]
  }
  edges: { source: number[]; target: number[]; label: number[]; confidence: Array<number | null> }
  edge_labels: string[]
}

/**
 * Back to the shape the views read. The object form is 12 MB for 3.5k nodes /
 * 63k edges (8.6 MB of it edge UUIDs); the compact form is about a seventh.
 * The server no longer sends edge ids — nothing reads them — so they are numbered here.
 */
export function expandCompactGraph(c: CompactGraph): GraphData {
  const n = c.nodes
  const nodes: GraphData['nodes'] = n.id.map((id, i) => ({
    data: {
      id,
      label: n.label[i],
      type: n.type[i],
      description: n.description[i] ?? undefined,
      mention_count: n.mention_count[i],
      x: n.x[i],
      y: n.y[i],
      community: n.community[i],
    },
  }))
  const e = c.edges
  const edges: GraphData['edges'] = e.source.map((s, i) => ({
    data: {
      id: `e${i}`,
      source: n.id[s],
      target: n.id[e.target[i]],
      label: c.edge_labels[e.label[i]] ?? '',
      confidence: e.confidence[i] ?? 0,
    },
  }))
  return { nodes, edges }
}

function isCompactGraph(data: unknown): data is CompactGraph {
  return typeof data === 'object' && data !== null && (data as { format?: unknown }).format === 'compact-1'
}

export function useGraph() {
  const graphData = ref<GraphData | null>(null)
  const communityGraphData = ref<CommunityGraphData | null>(null)
  const selectedNode = ref<NodeDetail | null>(null)
  const loading = ref(false)
  const nodeLoading = ref(false)

  async function fetchGraph(communityFilter?: number) {
    loading.value = true
    const params: Record<string, string> = { format: 'compact' }
    if (communityFilter !== undefined) {
      params.community = String(communityFilter)
    }
    const { data } = await api.get<GraphData | CompactGraph>('/api/graph', { params })
    // A backend that predates the compact form ignores the parameter and answers in the object form.
    graphData.value = isCompactGraph(data) ? expandCompactGraph(data) : data
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
