<template>
  <v-container fluid class="fill-height pa-0 d-flex flex-column">
    <!-- Toolbar -->
    <div class="d-flex align-center ga-2 px-4 py-2 toolbar">
      <!-- Type filter chips -->
      <div class="d-flex ga-1 flex-wrap">
        <button
          v-for="type in entityTypes"
          :key="type"
          class="type-pill"
          :class="{ active: isTypeActive(type) }"
          :style="{ '--pill-color': TYPE_COLORS[type] }"
          @click="toggleType(type)"
        >
          <span class="type-dot" />
          {{ type }}
        </button>
      </div>

      <v-spacer />

      <!-- Search -->
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        placeholder="Search nodes..."
        clearable
        style="max-width: 200px;"
      />

      <!-- Controls -->
      <div class="d-flex align-center ga-1">
        <v-btn icon="mdi-minus" variant="text" size="x-small" @click="zoomOut" />
        <v-btn icon="mdi-plus" variant="text" size="x-small" @click="zoomIn" />
        <v-btn icon="mdi-fit-to-screen-outline" variant="text" size="x-small" @click="resetCamera" />
        <v-btn icon="mdi-refresh" variant="text" size="x-small" @click="refreshGraph" />
      </div>
    </div>

    <!-- Status bar -->
    <div class="d-flex align-center ga-3 px-4 py-1 status-bar">
      <span class="text-caption text-medium-emphasis" style="font-variant-numeric: tabular-nums;">
        {{ nodeCount.toLocaleString() }} nodes &middot; {{ edgeCount.toLocaleString() }} edges
      </span>
      <span v-if="layoutRunning" class="layout-indicator">
        <span class="layout-dot" />
        converging
      </span>
      <v-spacer />
      <span v-if="focusedLabel" class="text-caption">
        <span class="text-medium-emphasis">focused:</span>
        <span class="ml-1 font-weight-medium">{{ focusedLabel }}</span>
      </span>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="d-flex align-center justify-center flex-grow-1 sigma-canvas">
      <div class="text-center">
        <v-progress-circular indeterminate color="primary" size="40" width="2" class="mb-3" />
        <div class="text-caption text-medium-emphasis">Loading graph...</div>
      </div>
    </div>

    <!-- Sigma Canvas -->
    <div v-show="!loading" ref="sigmaContainer" class="flex-grow-1 sigma-canvas" />

    <!-- Side Panel -->
    <GraphSidePanel :node="selectedNode" @close="handleClosePanel" />
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

// Muted, Obsidian-inspired palette
const TYPE_COLORS: Record<string, string> = {
  person: '#7f8cff',
  project: '#6bcb77',
  technology: '#a78bfa',
  concept: '#d4a056',
  file: '#6b7280',
  config: '#c4983c',
  error: '#e06060',
  location: '#6bcb77',
  organization: '#7f8cff',
}

const entityTypes = Object.keys(TYPE_COLORS)

const { graphData, selectedNode, loading, fetchGraph, fetchNodeDetail, clearSelection } = useGraph()

const sigmaContainer = ref<HTMLElement | null>(null)
const searchQuery = ref('')
const activeTypes = ref(new Set(entityTypes))
const layoutRunning = ref(false)
const nodeCount = ref(0)
const edgeCount = ref(0)
const focusedLabel = ref('')

let graph: Graph | null = null
let renderer: Sigma | null = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let fa2Layout: any = null
let focusedNode: string | null = null
const focusedNeighbors = new Set<string>()
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

function focusNode(nodeId: string) {
  focusedNode = nodeId
  focusedNeighbors.clear()
  graph!.forEachNeighbor(nodeId, (n) => focusedNeighbors.add(n))
  focusedLabel.value = graph!.getNodeAttribute(nodeId, 'label') || ''
  renderer?.refresh()
}

function unfocusNode() {
  focusedNode = null
  focusedNeighbors.clear()
  focusedLabel.value = ''
  renderer?.refresh()
}

function initSigma() {
  if (!sigmaContainer.value || !graphData.value) return

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
    const mc = node.data.mention_count || 1
    graph.addNode(node.data.id, {
      label: node.data.label,
      x: Math.random() * 100,
      y: Math.random() * 100,
      size: Math.max(2.5, Math.min(16, Math.sqrt(mc) * 2.5)),
      color: TYPE_COLORS[node.data.type] || '#6b7280',
      nodeType: node.data.type,
    })
  }

  for (const edge of graphData.value.edges) {
    if (graph.hasNode(edge.data.source) && graph.hasNode(edge.data.target)) {
      try {
        graph.addEdge(edge.data.source, edge.data.target, {
          label: edge.data.label,
          size: 0.4,
          color: 'rgba(255, 255, 255, 0.06)',
        })
      } catch {
        // duplicate edge
      }
    }
  }

  nodeCount.value = graph.order
  edgeCount.value = graph.size

  renderer = new Sigma(graph, sigmaContainer.value, {
    renderLabels: true,
    labelColor: { color: 'rgba(255, 255, 255, 0.7)' },
    labelSize: 11,
    labelFont: '"Inter", system-ui, sans-serif',
    labelWeight: '400',
    labelDensity: 0.06,
    labelGridCellSize: 80,
    labelRenderedSizeThreshold: 6,
    defaultNodeColor: '#6b7280',
    defaultEdgeColor: 'rgba(255, 255, 255, 0.06)',
    stagePadding: 40,
    zIndex: true,

    nodeReducer(node, data) {
      const res = { ...data }
      const type = graph!.getNodeAttribute(node, 'nodeType')

      // Type filter
      if (activeTypes.value.size < entityTypes.length && !activeTypes.value.has(type)) {
        res.hidden = true
        return res
      }

      // Search highlighting
      if (searchMatches.size > 0) {
        if (searchMatches.has(node)) {
          res.highlighted = true
          res.zIndex = 1
          res.color = lighten(res.color as string, 0.3)
        } else {
          res.color = 'rgba(255, 255, 255, 0.03)'
          res.label = ''
        }
        return res
      }

      // Click-focus highlighting
      if (focusedNode) {
        if (node === focusedNode) {
          res.highlighted = true
          res.zIndex = 2
          res.color = lighten(res.color as string, 0.4)
          res.size = (res.size as number) * 1.3
        } else if (focusedNeighbors.has(node)) {
          res.highlighted = true
          res.zIndex = 1
          res.color = lighten(res.color as string, 0.15)
        } else {
          res.color = 'rgba(255, 255, 255, 0.03)'
          res.label = ''
        }
      }

      return res
    },

    edgeReducer(edge, data) {
      const res = { ...data }

      if (focusedNode) {
        if (graph!.extremities(edge).includes(focusedNode)) {
          res.color = 'rgba(255, 255, 255, 0.25)'
          res.size = 1
          res.zIndex = 1
        } else {
          res.hidden = true
        }
      }

      if (searchMatches.size > 0 && !focusedNode) {
        const [src, tgt] = graph!.extremities(edge)
        if (!searchMatches.has(src) && !searchMatches.has(tgt)) {
          res.hidden = true
        } else {
          res.color = 'rgba(255, 255, 255, 0.15)'
        }
      }

      return res
    },
  })

  // Pointer cursor on node hover (no visual change)
  renderer.on('enterNode', () => {
    sigmaContainer.value!.style.cursor = 'pointer'
  })
  renderer.on('leaveNode', () => {
    sigmaContainer.value!.style.cursor = 'default'
  })

  // Click to focus + open side panel
  renderer.on('clickNode', async ({ node }) => {
    focusNode(node)
    await fetchNodeDetail(node)
  })

  // Click background to deselect
  renderer.on('clickStage', () => {
    unfocusNode()
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

// Lighten a hex color
function lighten(hex: string, amount: number): string {
  if (hex.startsWith('rgba')) return hex
  const h = hex.replace('#', '')
  const r = Math.min(255, parseInt(h.substring(0, 2), 16) + Math.round(255 * amount))
  const g = Math.min(255, parseInt(h.substring(2, 4), 16) + Math.round(255 * amount))
  const b = Math.min(255, parseInt(h.substring(4, 6), 16) + Math.round(255 * amount))
  return `rgb(${r}, ${g}, ${b})`
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

function handleClosePanel() {
  unfocusNode()
  clearSelection()
}

async function refreshGraph() {
  searchQuery.value = ''
  searchMatches.clear()
  activeTypes.value = new Set(entityTypes)
  unfocusNode()
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
  background: #101016;
}
.toolbar {
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  background: rgba(16, 16, 22, 0.6);
}
.status-bar {
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  background: rgba(16, 16, 22, 0.4);
}
.type-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: transparent;
  color: rgba(255, 255, 255, 0.4);
  cursor: pointer;
  transition: all 150ms ease;
  font-family: inherit;
}
.type-pill.active {
  color: var(--pill-color);
  border-color: color-mix(in srgb, var(--pill-color) 30%, transparent);
  background: color-mix(in srgb, var(--pill-color) 8%, transparent);
}
.type-pill:hover {
  border-color: rgba(255, 255, 255, 0.12);
}
.type-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--pill-color);
  opacity: 0.3;
  transition: opacity 150ms ease;
}
.type-pill.active .type-dot {
  opacity: 1;
}
.layout-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.35);
}
.layout-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #d4a056;
  animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}
</style>
