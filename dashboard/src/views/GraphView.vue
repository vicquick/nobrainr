<template>
  <v-container fluid class="fill-height pa-0 d-flex flex-column">
    <!-- Toolbar -->
    <div class="d-flex align-center ga-2 px-4 py-2" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
      <!-- Type filter chips -->
      <div class="d-flex ga-1 flex-wrap">
        <v-chip
          v-for="type in entityTypes"
          :key="type"
          :color="TYPE_COLORS[type]"
          :variant="isTypeActive(type) ? 'flat' : 'outlined'"
          size="x-small"
          class="type-chip"
          @click="toggleType(type)"
        >
          {{ type }}
        </v-chip>
      </div>

      <v-spacer />

      <!-- Search -->
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        placeholder="Search nodes..."
        clearable
        style="max-width: 220px;"
      />

      <!-- Controls -->
      <v-btn-group density="compact" variant="text" divided>
        <v-btn icon="mdi-minus" size="small" @click="zoomOut" />
        <v-btn icon="mdi-plus" size="small" @click="zoomIn" />
        <v-btn icon="mdi-fit-to-screen-outline" size="small" @click="resetCamera" />
      </v-btn-group>

      <v-btn icon="mdi-refresh" variant="text" size="small" @click="refreshGraph" />
    </div>

    <!-- Node count -->
    <div class="d-flex align-center ga-2 px-4 py-1" style="border-bottom: 1px solid rgba(255,255,255,0.04);">
      <span class="text-caption text-medium-emphasis">
        {{ nodeCount.toLocaleString() }} nodes &middot; {{ edgeCount.toLocaleString() }} edges
      </span>
      <v-chip v-if="layoutRunning" size="x-small" variant="tonal" color="warning" class="ml-1">
        <v-progress-circular indeterminate size="10" width="1" class="mr-1" />
        laying out
      </v-chip>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="d-flex align-center justify-center flex-grow-1">
      <div class="text-center">
        <v-progress-circular indeterminate color="primary" size="48" class="mb-3" />
        <div class="text-body-2 text-medium-emphasis">Loading knowledge graph...</div>
      </div>
    </div>

    <!-- Sigma Canvas -->
    <div v-show="!loading" ref="sigmaContainer" class="flex-grow-1 sigma-canvas" />

    <!-- Side Panel -->
    <GraphSidePanel :node="selectedNode" @close="clearSelection" />
  </v-container>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import Sigma from 'sigma'
import Graph from 'graphology'
import FA2Layout from 'graphology-layout-forceatlas2/worker'
import { useGraph } from '@/composables/useGraph'
import { useSSE } from '@/composables/useSSE'
import GraphSidePanel from '@/components/GraphSidePanel.vue'

const TYPE_COLORS: Record<string, string> = {
  person: '#58a6ff',
  project: '#3fb950',
  technology: '#bc8cff',
  concept: '#f0883e',
  file: '#8b949e',
  config: '#d29922',
  error: '#f85149',
  location: '#3fb950',
  organization: '#58a6ff',
}

const entityTypes = Object.keys(TYPE_COLORS)

const { graphData, selectedNode, loading, fetchGraph, fetchNodeDetail, clearSelection } = useGraph()

const sigmaContainer = ref<HTMLElement | null>(null)
const searchQuery = ref('')
const activeTypes = ref(new Set(entityTypes))
const layoutRunning = ref(false)
const nodeCount = ref(0)
const edgeCount = ref(0)

let graph: Graph | null = null
let renderer: Sigma | null = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let fa2Layout: any = null
let hoveredNode: string | null = null
const hoveredNeighbors = new Set<string>()
const searchMatches = new Set<string>()

function isTypeActive(type: string) {
  return activeTypes.value.has(type)
}

function toggleType(type: string) {
  const next = new Set(activeTypes.value)
  if (next.has(type)) {
    next.delete(type)
  } else {
    next.add(type)
  }
  activeTypes.value = next
  renderer?.refresh()
}

function initSigma() {
  if (!sigmaContainer.value || !graphData.value) return

  // Cleanup previous
  if (fa2Layout) {
    fa2Layout.stop()
    fa2Layout.kill()
    fa2Layout = null
  }
  if (renderer) {
    renderer.kill()
    renderer = null
  }

  graph = new Graph()

  for (const node of graphData.value.nodes) {
    graph.addNode(node.data.id, {
      label: node.data.label,
      x: Math.random() * 100,
      y: Math.random() * 100,
      size: Math.max(3, Math.min(20, Math.sqrt(node.data.mention_count || 1) * 3)),
      color: TYPE_COLORS[node.data.type] || '#8b949e',
      nodeType: node.data.type,
    })
  }

  for (const edge of graphData.value.edges) {
    if (graph.hasNode(edge.data.source) && graph.hasNode(edge.data.target)) {
      try {
        graph.addEdge(edge.data.source, edge.data.target, {
          label: edge.data.label,
          size: Math.max(0.5, (edge.data.confidence || 0.5) * 2),
          color: 'rgba(48, 54, 61, 0.5)',
        })
      } catch {
        // duplicate edge — skip
      }
    }
  }

  nodeCount.value = graph.order
  edgeCount.value = graph.size

  renderer = new Sigma(graph, sigmaContainer.value, {
    renderLabels: true,
    labelColor: { color: '#c9d1d9' },
    labelSize: 12,
    labelFont: '"Inter", system-ui, sans-serif',
    labelDensity: 0.07,
    labelGridCellSize: 60,
    labelRenderedSizeThreshold: 5,
    defaultNodeColor: '#8b949e',
    defaultEdgeColor: 'rgba(48, 54, 61, 0.5)',
    stagePadding: 30,
    zIndex: true,
    nodeReducer(node, data) {
      const res = { ...data }
      const type = graph!.getNodeAttribute(node, 'nodeType')

      // Type filter
      if (activeTypes.value.size < entityTypes.length && !activeTypes.value.has(type)) {
        res.hidden = true
        return res
      }

      // Search
      if (searchMatches.size > 0) {
        if (searchMatches.has(node)) {
          res.highlighted = true
          res.zIndex = 1
        } else {
          res.color = '#1a1a2e'
          res.label = ''
        }
        return res
      }

      // Hover
      if (hoveredNode) {
        if (node === hoveredNode) {
          res.highlighted = true
          res.zIndex = 2
        } else if (hoveredNeighbors.has(node)) {
          res.highlighted = true
          res.zIndex = 1
        } else {
          res.color = '#0d1117'
          res.label = ''
        }
      }

      return res
    },
    edgeReducer(edge, data) {
      const res = { ...data }

      if (hoveredNode) {
        if (graph!.extremities(edge).includes(hoveredNode)) {
          res.color = '#58a6ff'
          res.size = 2
          res.zIndex = 1
        } else {
          res.hidden = true
        }
      }

      if (searchMatches.size > 0 && !hoveredNode) {
        const [src, tgt] = graph!.extremities(edge)
        if (!searchMatches.has(src) && !searchMatches.has(tgt)) {
          res.hidden = true
        }
      }

      return res
    },
  })

  renderer.on('enterNode', ({ node }) => {
    hoveredNode = node
    hoveredNeighbors.clear()
    graph!.forEachNeighbor(node, (n) => hoveredNeighbors.add(n))
    sigmaContainer.value!.style.cursor = 'pointer'
    renderer!.refresh()
  })

  renderer.on('leaveNode', () => {
    hoveredNode = null
    hoveredNeighbors.clear()
    sigmaContainer.value!.style.cursor = 'default'
    renderer!.refresh()
  })

  renderer.on('clickNode', async ({ node }) => {
    await fetchNodeDetail(node)
  })

  renderer.on('clickStage', () => {
    clearSelection()
  })

  // ForceAtlas2 in web worker
  layoutRunning.value = true
  fa2Layout = new FA2Layout(graph, {
    settings: {
      gravity: 0.5,
      scalingRatio: 10,
      barnesHutOptimize: true,
      barnesHutTheta: 0.5,
      slowDown: 5,
      strongGravityMode: false,
      linLogMode: false,
      outboundAttractionDistribution: false,
      adjustSizes: false,
      edgeWeightInfluence: 1,
    },
  })
  fa2Layout.start()

  setTimeout(() => {
    if (fa2Layout) {
      fa2Layout.stop()
      layoutRunning.value = false
    }
  }, 8000)
}

// Debounced search
let searchTimeout: ReturnType<typeof setTimeout>
watch(searchQuery, (q) => {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    searchMatches.clear()
    if (q && graph) {
      const lower = q.toLowerCase()
      graph.forEachNode((node, attrs) => {
        if (attrs.label?.toLowerCase().includes(lower)) {
          searchMatches.add(node)
        }
      })
    }
    renderer?.refresh()
  }, 200)
})

function zoomIn() {
  renderer?.getCamera().animatedZoom({ duration: 300 })
}

function zoomOut() {
  renderer?.getCamera().animatedUnzoom({ duration: 300 })
}

function resetCamera() {
  renderer?.getCamera().animatedReset({ duration: 400 })
}

async function refreshGraph() {
  searchQuery.value = ''
  searchMatches.clear()
  activeTypes.value = new Set(entityTypes)
  hoveredNode = null
  hoveredNeighbors.clear()
  clearSelection()
  await fetchGraph()
  await nextTick()
  initSigma()
}

useSSE(async (evt) => {
  if (['memory_created', 'memory_deleted'].includes(evt.type)) {
    await fetchGraph()
    await nextTick()
    initSigma()
  }
})

onMounted(async () => {
  await fetchGraph()
  await nextTick()
  initSigma()
})

onUnmounted(() => {
  if (fa2Layout) {
    fa2Layout.stop()
    fa2Layout.kill()
  }
  renderer?.kill()
})
</script>

<style scoped>
.sigma-canvas {
  width: 100%;
  background: #0a0e14;
}
.type-chip {
  cursor: pointer;
  transition: all 150ms ease;
  font-weight: 500;
  letter-spacing: 0;
}
.type-chip:hover {
  transform: scale(1.05);
}
</style>
