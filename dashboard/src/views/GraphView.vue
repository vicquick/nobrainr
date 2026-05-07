<template>
  <v-container fluid class="fill-height pa-0 d-flex flex-column">
    <!-- Compact toolbar -->
    <div class="d-flex align-center ga-2 px-3 py-1 toolbar">
      <!-- View mode toggle -->
      <v-btn-toggle v-model="viewMode" mandatory density="compact" variant="outlined" class="view-toggle mr-2">
        <v-btn value="communities" size="x-small">
          <v-icon size="14" class="mr-1">mdi-circle-multiple-outline</v-icon>
          Topics
        </v-btn>
        <v-btn value="entities" size="x-small">
          <v-icon size="14" class="mr-1">mdi-graph-outline</v-icon>
          Entities
        </v-btn>
      </v-btn-toggle>

      <!-- Back button when drilled into a community -->
      <v-btn
        v-if="drillCommunityId !== null"
        size="x-small"
        variant="tonal"
        color="primary"
        @click="exitDrill"
        class="mr-2"
      >
        <v-icon size="14" class="mr-1">mdi-arrow-left</v-icon>
        All topics
      </v-btn>

      <div v-if="viewMode === 'entities'" class="pills-scroll">
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
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        placeholder="Search..."
        clearable
        style="max-width: 180px;"
        density="compact"
      />
      <div class="d-flex align-center ga-0">
        <v-btn icon="mdi-minus" variant="text" size="x-small" aria-label="Zoom out" @click="zoomOut" />
        <v-btn icon="mdi-plus" variant="text" size="x-small" aria-label="Zoom in" @click="zoomIn" />
        <v-btn icon="mdi-fit-to-screen-outline" variant="text" size="x-small" aria-label="Fit graph to screen" @click="resetCamera" />
        <v-btn icon="mdi-refresh" variant="text" size="x-small" aria-label="Refresh graph data" @click="refreshGraph" />
      </div>
    </div>

    <!-- Status bar -->
    <div class="d-flex align-center ga-3 px-3 py-0 status-bar">
      <span class="text-caption text-medium-emphasis" style="font-variant-numeric: tabular-nums; font-size: 10px;">
        <template v-if="viewMode === 'communities'">
          {{ nodeCount.toLocaleString() }} topics · {{ edgeCount.toLocaleString() }} connections
        </template>
        <template v-else>
          {{ nodeCount.toLocaleString() }} nodes · {{ edgeCount.toLocaleString() }} edges · {{ communityCount }} clusters
          <span v-if="drillCommunityId !== null" class="ml-1">(filtered to 1 topic)</span>
        </template>
        <span v-if="graphStale" class="ml-2" style="color: #c4a46a;">· updated available</span>
      </span>
      <v-spacer />
      <span v-if="focusedLabel" class="text-caption" style="font-size: 10px;">
        <span class="text-medium-emphasis">focused:</span>
        <span class="ml-1 font-weight-medium">{{ focusedLabel }}</span>
      </span>
    </div>

    <!-- Canvas + Entity Side Panel -->
    <div class="graph-area" :class="{ 'panel-open': panelOpen }">
      <div ref="sigmaContainer" class="sigma-canvas" />
      <!-- Loading overlay (on top of canvas so container always has dimensions) -->
      <div v-if="loading" class="loading-overlay">
        <div class="text-center">
          <v-progress-circular indeterminate color="primary" size="40" width="2" class="mb-3" />
          <div class="text-caption text-medium-emphasis">Loading graph...</div>
        </div>
      </div>
      <div v-if="panelOpen" class="entity-panel">
        <GraphSidePanel :node="selectedNode" @close="handleClosePanel" @navigate="handleNavigateEntity" />
      </div>
    </div>

    <!-- Mobile bottom sheet for entity panel -->
    <v-bottom-sheet v-if="mobile" v-model="showMobilePanel" :scrim="false">
      <v-card color="#12121a" class="mobile-entity-sheet" rounded="t-xl">
        <div class="sheet-handle" />
        <GraphSidePanel :node="selectedNode" @close="handleClosePanel" @navigate="handleNavigateEntity" />
      </v-card>
    </v-bottom-sheet>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useDisplay } from 'vuetify'
import Sigma from 'sigma'
import Graph from 'graphology'
import { EdgeLineProgram } from 'sigma/rendering'
import { createNodeBorderProgram } from '@sigma/node-border'
import { useGraph } from '@/composables/useGraph'
import { useSSE } from '@/composables/useSSE'
import { useChatStore } from '@/stores/chat'
import GraphSidePanel from '@/components/GraphSidePanel.vue'

const TYPE_COLORS: Record<string, string> = {
  person: '#5c7cfacc',
  project: '#2f9e44cc',
  technology: '#7048e8cc',
  concept: '#e67700cc',
  file: '#6b7280aa',
  config: '#d9480fcc',
  error: '#e03131cc',
  location: '#0ca678cc',
  organization: '#1971c2cc',
}

const entityTypes = Object.keys(TYPE_COLORS)
const { mobile } = useDisplay()
const chatStore = useChatStore()

const { graphData, communityGraphData, selectedNode, loading, fetchGraph, fetchCommunityGraph, fetchNodeDetail, clearSelection } = useGraph()

const sigmaContainer = ref<HTMLElement | null>(null)
const searchQuery = ref('')
const activeTypes = ref(new Set(entityTypes))
const nodeCount = ref(0)
const edgeCount = ref(0)
const communityCount = ref(0)
const focusedLabel = ref('')
const viewMode = ref<'communities' | 'entities'>('entities')
const drillCommunityId = ref<number | null>(null)

const panelOpen = computed(() => !!selectedNode.value && !mobile.value)
const showMobilePanel = computed({
  get: () => mobile.value && !!selectedNode.value,
  set: (v) => { if (!v) handleClosePanel() },
})

let graph: Graph | null = null
let renderer: Sigma | null = null
let resizeObserver: ResizeObserver | null = null

// Custom label renderer with dark background plate
function drawLabelWithBg(
  context: CanvasRenderingContext2D,
  data: Record<string, any>,
  settings: Record<string, any>,
): void {
  if (!data.label) return
  const size = settings.labelSize
  const font = settings.labelFont
  const weight = settings.labelWeight
  const color = data.labelColor || 'rgba(255, 255, 255, 0.88)'
  const bgColor = data.labelBgColor || 'rgba(10, 10, 14, 0.75)'

  context.font = `${weight} ${size}px ${font}`
  const textWidth = context.measureText(data.label).width
  const x = data.x + data.size + 3
  const y = data.y + size / 3

  // Background plate — tight to text
  const px = 4, r = 3
  const rx = x - px, ry = y - size + 1
  const rw = textWidth + px * 2, rh = size + 3
  context.fillStyle = bgColor
  context.beginPath()
  context.moveTo(rx + r, ry)
  context.lineTo(rx + rw - r, ry)
  context.quadraticCurveTo(rx + rw, ry, rx + rw, ry + r)
  context.lineTo(rx + rw, ry + rh - r)
  context.quadraticCurveTo(rx + rw, ry + rh, rx + rw - r, ry + rh)
  context.lineTo(rx + r, ry + rh)
  context.quadraticCurveTo(rx, ry + rh, rx, ry + rh - r)
  context.lineTo(rx, ry + r)
  context.quadraticCurveTo(rx, ry, rx + r, ry)
  context.closePath()
  context.fill()

  context.fillStyle = color
  context.fillText(data.label, x, y)
}

// Node program with subtle border for depth
const BorderedNodeProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.12 }, color: { value: '#2a2a36' } },
    { size: { fill: true }, color: { attribute: 'color' } },
  ],
  drawLabel: drawLabelWithBg,
})

let focusedNode: string | null = null
let hoveredNode: string | null = null
const hoveredNeighbors = new Set<string>()
const focusedNeighbors = new Set<string>()
const searchMatches = new Set<string>()
const hubNodes = new Set<string>()
const chatFocusedNodes = new Set<string>()
const chatFocusedNeighbors = new Set<string>()

function isTypeActive(type: string) {
  return activeTypes.value.has(type)
}

function toggleType(type: string) {
  const next = new Set(activeTypes.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  activeTypes.value = next
  renderer?.refresh()
}

function zoomToNodes(nodeIds: Set<string> | string[]) {
  if (!graph || !renderer) return
  const ids = (nodeIds instanceof Set ? [...nodeIds] : nodeIds).filter(id => graph!.hasNode(id))
  if (ids.length === 0) return

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
  for (const id of ids) {
    const d = renderer.getNodeDisplayData(id)
    if (!d) continue
    if (d.x < minX) minX = d.x
    if (d.x > maxX) maxX = d.x
    if (d.y < minY) minY = d.y
    if (d.y > maxY) maxY = d.y
  }

  if (minX === Infinity) return

  const cx = (minX + maxX) / 2
  const cy = (minY + maxY) / 2
  const dx = maxX - minX
  const dy = maxY - minY
  const { width, height } = renderer.getDimensions()
  const aspect = width / height
  const padding = 1.5
  const ratioForWidth = (dx * padding) / aspect
  const ratioForHeight = dy * padding
  const newRatio = Math.max(ratioForWidth, ratioForHeight, 0.1)

  renderer.getCamera().animate(
    { x: cx, y: cy, ratio: Math.max(0.05, Math.min(newRatio, 2)) },
    { duration: 400 },
  )
}

function focusNode(nodeId: string) {
  focusedNode = nodeId
  focusedNeighbors.clear()
  chatFocusedNodes.clear()
  chatFocusedNeighbors.clear()
  graph!.forEachNeighbor(nodeId, (n) => focusedNeighbors.add(n))
  focusedLabel.value = graph!.getNodeAttribute(nodeId, 'label') || ''
  renderer?.refresh()
  zoomToNodes(new Set([nodeId, ...focusedNeighbors]))
}

function unfocusNode() {
  focusedNode = null
  focusedNeighbors.clear()
  focusedLabel.value = ''
  renderer?.refresh()
}

function initSigma() {
  if (!sigmaContainer.value || !graphData.value) return

  if (renderer) {
    renderer.kill()
    renderer = null
  }

  // Clear stale highlight state — node IDs from previous graph may not exist
  chatFocusedNodes.clear()
  chatFocusedNeighbors.clear()
  focusedNode = null
  focusedNeighbors.clear()
  focusedLabel.value = ''

  graph = new Graph()

  const communities = new Set<number>()

  // When drilled into a community, only show that community's entities
  const drillFilter = drillCommunityId.value
  const filteredNodes = drillFilter !== null
    ? graphData.value.nodes.filter(n => n.data.community === drillFilter)
    : graphData.value.nodes
  const filteredNodeIds = new Set(filteredNodes.map(n => n.data.id))

  for (const node of filteredNodes) {
    const mc = node.data.mention_count || 1
    communities.add(node.data.community)
    graph.addNode(node.data.id, {
      label: node.data.label,
      x: node.data.x,
      y: node.data.y,
      size: Math.max(2, Math.min(28, Math.pow(mc, 0.38) * 1.1)),
      color: TYPE_COLORS[node.data.type] || '#6b7280aa',
      labelColor: 'rgba(255, 255, 255, 0.7)',
      nodeType: node.data.type,
      community: node.data.community,
    })
  }

  const filteredEdges = drillFilter !== null
    ? graphData.value.edges.filter(e => filteredNodeIds.has(e.data.source) && filteredNodeIds.has(e.data.target))
    : graphData.value.edges

  for (const edge of filteredEdges) {
    if (graph.hasNode(edge.data.source) && graph.hasNode(edge.data.target)) {
      try {
        graph.addEdge(edge.data.source, edge.data.target, {
          label: edge.data.label,
          size: 0.8,
          color: '#242438',
        })
      } catch {
        // duplicate edge
      }
    }
  }

  nodeCount.value = graph.order
  edgeCount.value = graph.size
  communityCount.value = communities.size

  // Identify hub nodes for edge filtering at overview level
  hubNodes.clear()
  graph.forEachNode((node) => {
    if (graph!.degree(node) >= 10) hubNodes.add(node)
  })

  renderer = new Sigma(graph, sigmaContainer.value, {
    // Edge rendering — gl.LINES for performance
    defaultEdgeType: 'line',
    edgeProgramClasses: { line: EdgeLineProgram },

    // Performance
    enableEdgeEvents: false,

    // Labels
    drawLabel: drawLabelWithBg,
    renderLabels: true,
    labelColor: { attribute: 'labelColor', defaultValue: 'rgba(255, 255, 255, 0.7)' },
    labelSize: 11,
    labelFont: '"Inter", system-ui, sans-serif',
    labelWeight: '500',
    labelDensity: 0.12,
    labelGridCellSize: 100,
    labelRenderedSizeThreshold: 5,

    // Defaults
    defaultNodeColor: '#6b7280aa',
    defaultEdgeColor: '#242438',
    stagePadding: 40,
    zIndex: true,
    enableNodeHoverHighlighting: false,

    nodeReducer(node, data) {
      const res = { ...data }
      const type = graph!.getNodeAttribute(node, 'nodeType')

      // Type filter
      if (activeTypes.value.size < entityTypes.length && !activeTypes.value.has(type)) {
        res.hidden = true
        return res
      }

      // Click-focus takes priority
      if (focusedNode) {
        if (node === focusedNode) {
          res.zIndex = 2
          res.size = (res.size as number) * 1.6
          res.forceLabel = true
          res.labelColor = '#000000'
          res.labelBgColor = 'rgba(255, 255, 255, 0.95)'
        } else if (focusedNeighbors.has(node)) {
          res.zIndex = 1
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.92)'
          res.labelBgColor = 'rgba(10, 10, 14, 0.82)'
        } else {
          res.color = 'rgba(60, 60, 70, 0.15)'
          res.size = 1.5
          res.label = ''
        }
        return res
      }

      // Chat focus: entities from chatbot response — same visual treatment as click-focus
      if (chatFocusedNodes.size > 0) {
        if (chatFocusedNodes.has(node)) {
          res.zIndex = 2
          res.size = (res.size as number) * 1.6
          res.forceLabel = true
          res.labelColor = '#000000'
          res.labelBgColor = 'rgba(255, 255, 255, 0.95)'
        } else if (chatFocusedNeighbors.has(node)) {
          res.zIndex = 1
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.92)'
          res.labelBgColor = 'rgba(10, 10, 14, 0.82)'
        } else {
          res.color = 'rgba(60, 60, 70, 0.15)'
          res.size = 1.5
          res.label = ''
        }
        return res
      }

      // Search: highlight matches, dim others
      if (searchMatches.size > 0) {
        if (searchMatches.has(node)) {
          res.zIndex = 1
          res.color = lighten(res.color as string, 0.3)
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.9)'
        } else {
          res.color = 'rgba(60, 60, 70, 0.15)'
          res.size = 1.5
          res.label = ''
        }
        return res
      }

      // Hover: show label with high contrast
      if (hoveredNode === node) {
        res.forceLabel = true
        res.labelColor = '#000000'
        res.labelBgColor = 'rgba(255, 255, 255, 0.92)'
      }

      return res
    },

    edgeReducer(edge, data) {
      const res = { ...data }

      // Click-focus: show only edges to focused node
      if (focusedNode) {
        if (graph!.extremities(edge).includes(focusedNode)) {
          res.color = '#5c5c8a'
          res.size = 1.5
          res.zIndex = 1
        } else {
          res.hidden = true
        }
        return res
      }

      // Chat focus: show edges where at least one extremity is chat-focused
      if (chatFocusedNodes.size > 0) {
        const [src, tgt] = graph!.extremities(edge)
        if (chatFocusedNodes.has(src) || chatFocusedNodes.has(tgt)) {
          res.color = '#5c5c8a'
          res.size = 1.5
          res.zIndex = 1
        } else {
          res.hidden = true
        }
        return res
      }

      // Search: show only edges between matches
      if (searchMatches.size > 0) {
        const [src, tgt] = graph!.extremities(edge)
        if (!searchMatches.has(src) || !searchMatches.has(tgt)) {
          res.hidden = true
        } else {
          res.color = '#3a3a5c'
        }
        return res
      }

      // Hover (with no active selection): light up edges touching
      // the hovered node so the local subgraph reads as a unit.
      // Other edges fall through to the hub-only filter below.
      if (hoveredNode) {
        const [src, tgt] = graph!.extremities(edge)
        if (src === hoveredNode || tgt === hoveredNode) {
          res.color = '#5c5c8a'
          res.size = 1.5
          res.zIndex = 1
          return res
        }
      }

      // Default overview: only show edges between hub nodes
      const [src, tgt] = graph!.extremities(edge)
      if (!hubNodes.has(src) || !hubNodes.has(tgt)) {
        res.hidden = true
      }

      return res
    },
  })

  // Hover events
  renderer.on('enterNode', ({ node }) => {
    hoveredNode = node
    hoveredNeighbors.clear()
    if (graph) graph.forEachNeighbor(node, (n) => hoveredNeighbors.add(n))
    sigmaContainer.value!.style.cursor = 'pointer'
    renderer?.refresh()
  })
  renderer.on('leaveNode', () => {
    hoveredNode = null
    hoveredNeighbors.clear()
    sigmaContainer.value!.style.cursor = 'default'
    renderer?.refresh()
  })

  // Click to focus + open side panel
  renderer.on('clickNode', async ({ node }) => {
    focusNode(node)
    await fetchNodeDetail(node)
  })

  // Click background to deselect — clear all highlights, restore full graph
  renderer.on('clickStage', () => {
    unfocusNode()
    clearSelection()
    chatFocusedNodes.clear()
    chatFocusedNeighbors.clear()
    renderer?.refresh()
  })
}

function initCommunitySigma() {
  if (!sigmaContainer.value || !communityGraphData.value) return

  if (renderer) {
    renderer.kill()
    renderer = null
  }
  focusedNode = null
  focusedNeighbors.clear()
  focusedLabel.value = ''
  chatFocusedNodes.clear()
  chatFocusedNeighbors.clear()

  graph = new Graph()

  for (const node of communityGraphData.value.nodes) {
    const size = Math.max(8, Math.min(60, Math.sqrt(node.data.size) * 3))
    graph.addNode(node.data.id, {
      label: node.data.label,
      x: node.data.x,
      y: node.data.y,
      size,
      color: node.data.color,
      labelColor: 'rgba(255, 255, 255, 0.85)',
      nodeType: node.data.type,
      community: node.data.community_id,
      communityId: node.data.community_id,
      memberCount: node.data.member_count,
      summary: node.data.summary,
      topEntities: node.data.top_entities,
    })
  }

  for (const edge of communityGraphData.value.edges) {
    if (graph.hasNode(edge.data.source) && graph.hasNode(edge.data.target)) {
      try {
        graph.addEdge(edge.data.source, edge.data.target, {
          size: Math.max(0.8, Math.min(3, Math.sqrt(edge.data.weight) * 0.5)),
          color: '#26263e',
        })
      } catch { /* dup */ }
    }
  }

  nodeCount.value = graph.order
  edgeCount.value = graph.size

  renderer = new Sigma(graph, sigmaContainer.value, {
    defaultNodeType: 'bordered',
    nodeProgramClasses: { bordered: BorderedNodeProgram },
    defaultEdgeType: 'line',
    edgeProgramClasses: { line: EdgeLineProgram },
    enableEdgeEvents: false,
    drawLabel: drawLabelWithBg,
    renderLabels: true,
    labelColor: { attribute: 'labelColor', defaultValue: 'rgba(255, 255, 255, 0.85)' },
    labelSize: 13,
    labelFont: '"Inter", system-ui, sans-serif',
    labelWeight: '600',
    labelDensity: 0.5,
    labelGridCellSize: 120,
    labelRenderedSizeThreshold: 4,
    defaultNodeColor: '#6b7280',
    defaultEdgeColor: '#26263e',
    stagePadding: 60,
    zIndex: true,
    enableNodeHoverHighlighting: false,

    nodeReducer(node, data) {
      const res = { ...data }
      // Hover takes precedence in the community overview — when nothing
      // is selected, hovering a community should pop it forward and
      // gently quiet the rest of the field.
      if (hoveredNode) {
        if (hoveredNode === node) {
          res.zIndex = 2
          res.size = (res.size as number) * 1.18
          res.forceLabel = true
          res.labelColor = '#000000'
          res.labelBgColor = 'rgba(255, 255, 255, 0.95)'
        } else if (hoveredNeighbors.has(node)) {
          res.zIndex = 1
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.92)'
        } else {
          res.color = 'rgba(60, 60, 70, 0.30)'
          res.label = ''
        }
      }
      // Search filter
      if (searchMatches.size > 0) {
        if (searchMatches.has(node)) {
          res.forceLabel = true
          res.zIndex = 1
        } else {
          res.color = 'rgba(60, 60, 70, 0.15)'
          res.size = 3
          res.label = ''
        }
      }
      return res
    },

    edgeReducer(edge, data) {
      const res = { ...data }
      if (searchMatches.size > 0) {
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
    if (graph) graph.forEachNeighbor(node, (n) => hoveredNeighbors.add(n))
    sigmaContainer.value!.style.cursor = 'pointer'
    renderer?.refresh()
  })
  renderer.on('leaveNode', () => {
    hoveredNode = null
    hoveredNeighbors.clear()
    sigmaContainer.value!.style.cursor = 'default'
    renderer?.refresh()
  })

  // Click community → drill into entity view for that community
  renderer.on('clickNode', async ({ node }) => {
    const communityId = graph!.getNodeAttribute(node, 'communityId')
    if (communityId !== undefined) {
      drillIntoCommunity(communityId)
    }
  })

  renderer.on('clickStage', () => {
    // nothing to do in community view
  })
}

async function drillIntoCommunity(communityId: number) {
  drillCommunityId.value = communityId
  viewMode.value = 'entities'
  // fetchGraph will load full graph; we filter in initSigma via drillCommunityId
  await fetchGraph()
  await nextTick()
  requestAnimationFrame(() => {
    initSigma()
    loading.value = false
  })
}

async function exitDrill() {
  drillCommunityId.value = null
  viewMode.value = 'communities'
  await switchToCommunityView()
}

async function switchToCommunityView() {
  loading.value = true
  await fetchCommunityGraph()
  await nextTick()
  requestAnimationFrame(() => {
    initCommunitySigma()
    loading.value = false
  })
}

async function switchToEntityView() {
  loading.value = true
  await fetchGraph()
  await nextTick()
  requestAnimationFrame(() => {
    initSigma()
    loading.value = false
  })
}

function lighten(hex: string, amount: number): string {
  if (hex.startsWith('rgba') || hex.startsWith('rgb(')) return hex
  const h = hex.replace('#', '')
  const r = Math.min(255, parseInt(h.substring(0, 2), 16) + Math.round(255 * amount))
  const g = Math.min(255, parseInt(h.substring(2, 4), 16) + Math.round(255 * amount))
  const b = Math.min(255, parseInt(h.substring(4, 6), 16) + Math.round(255 * amount))
  return `rgb(${r}, ${g}, ${b})`
}

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

// Navigate to a connected entity from side panel — focus + load details
async function handleNavigateEntity(entityId: string) {
  if (!entityId || !graph) return
  if (graph.hasNode(entityId)) {
    focusNode(entityId)
  }
  await fetchNodeDetail(entityId)
}

// Watch single entity focus from chat — click-focus on graph + open side panel
watch(() => chatStore.focusEntityId, async (entityId) => {
  if (!entityId || !graph || !graph.hasNode(entityId)) return
  focusNode(entityId)
  await fetchNodeDetail(entityId)
  chatStore.clearFocus()
})

// Watch chat sources — populate chatFocusedNodes with full click-focus treatment
watch(() => chatStore.currentSources, async (sources) => {
  if (!sources || !graph) return

  // Clear single click-focus to avoid conflicts
  focusedNode = null
  focusedNeighbors.clear()
  clearSelection()

  // Purge any stale IDs from previous graph reloads
  for (const id of chatFocusedNodes) {
    if (!graph.hasNode(id)) chatFocusedNodes.delete(id)
  }

  let added = false
  let firstEntityId: string | null = null
  for (const entity of sources.entities) {
    if (graph.hasNode(entity.id) && !chatFocusedNodes.has(entity.id)) {
      chatFocusedNodes.add(entity.id)
      if (!firstEntityId) firstEntityId = entity.id
      added = true
    }
  }

  // Compute neighbors: union of all neighbors of all chat-focused nodes
  chatFocusedNeighbors.clear()
  for (const nodeId of chatFocusedNodes) {
    if (!graph.hasNode(nodeId)) continue
    graph.forEachNeighbor(nodeId, (n) => {
      if (!chatFocusedNodes.has(n)) chatFocusedNeighbors.add(n)
    })
  }

  // Update status bar
  focusedLabel.value = chatFocusedNodes.size > 0
    ? `${chatFocusedNodes.size} chat entities`
    : ''

  if (chatFocusedNodes.size > 0) {
    renderer?.refresh()
    if (added) zoomToNodes(chatFocusedNodes)
    // Open side panel for the first new entity
    if (firstEntityId) {
      await fetchNodeDetail(firstEntityId)
    }
  } else {
    renderer?.refresh()
  }
})

// Clear conversation highlights when chat history is cleared
watch(() => chatStore.messages.length, (len) => {
  if (len === 0) {
    chatFocusedNodes.clear()
    chatFocusedNeighbors.clear()
    focusedLabel.value = ''
    renderer?.refresh()
  }
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
  chatFocusedNodes.clear()
  chatFocusedNeighbors.clear()
  activeTypes.value = new Set(entityTypes)
  graphStale.value = false
  drillCommunityId.value = null
  unfocusNode()
  clearSelection()
  if (viewMode.value === 'communities') {
    await switchToCommunityView()
  } else {
    await switchToEntityView()
  }
}

// Watch view mode changes (from toggle button)
watch(viewMode, async (mode) => {
  if (mode === 'communities' && drillCommunityId.value === null) {
    await switchToCommunityView()
  } else if (mode === 'entities' && drillCommunityId.value === null) {
    await switchToEntityView()
  }
})

// SSE: don't rebuild the graph while the user is looking at it — just track staleness
const graphStale = ref(false)
useSSE((evt) => {
  if (['memory_created', 'memory_deleted'].includes(evt.type)) {
    graphStale.value = true
  }
})

// Wait until the container has non-zero dimensions (flex layout settled)
function waitForLayout(): Promise<void> {
  return new Promise((resolve) => {
    const check = () => {
      const rect = sigmaContainer.value?.getBoundingClientRect()
      if (rect && rect.width > 0 && rect.height > 0) {
        resolve()
      } else {
        requestAnimationFrame(check)
      }
    }
    requestAnimationFrame(check)
  })
}

onMounted(async () => {
  if (viewMode.value === 'communities') {
    await fetchCommunityGraph()
  } else {
    await fetchGraph()
  }
  await nextTick()
  await waitForLayout()
  if (viewMode.value === 'communities') {
    initCommunitySigma()
  } else {
    initSigma()
  }
  loading.value = false

  // ResizeObserver: auto-resize Sigma when container changes (panel open/close, window resize)
  if (sigmaContainer.value) {
    resizeObserver = new ResizeObserver(() => {
      renderer?.resize()
      renderer?.refresh()
    })
    resizeObserver.observe(sigmaContainer.value)
  }
})

onUnmounted(() => {
  renderer?.kill()
  resizeObserver?.disconnect()
})
</script>

<style scoped>
/* Graph view chrome dressed in parchment palette. Sigma canvas itself
   keeps its dark fill — the graph nodes/edges are coloured per entity
   type by the renderer, not by CSS. */
.graph-area {
  --panel-width: 420px;
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  flex: 1;
  width: 100%;
  position: relative;
  min-height: 0;
  box-sizing: border-box;
  transition: padding-right 250ms ease;
  overflow: hidden;
}
.graph-area.panel-open {
  padding-right: var(--panel-width);
}
.sigma-canvas {
  width: 100%;
  height: 100%;
  background: #0e0b06;
}
.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(14, 11, 6, 0.95);
  z-index: 10;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  font-style: italic;
  color: rgba(238, 224, 196, 0.65);
}
.loading-overlay :deep(.v-progress-circular) { color: #c8a96e !important; }
.entity-panel {
  position: absolute;
  right: 0;
  top: 0;
  bottom: 0;
  width: var(--panel-width);
  background: linear-gradient(180deg, #14110a 0%, #0e0b06 100%);
  border-left: 1px solid rgba(200, 169, 110, 0.18);
  overflow-y: auto;
  z-index: 5;
  font-family: Georgia, serif;
}
/* Tablet: narrower panel */
@media (min-width: 600px) and (max-width: 960px) {
  .graph-area { --panel-width: 340px; }
}
@media (max-width: 960px) {
  .graph-area { transition: none; }
}
/* Small mobile: hide search field, let pills-scroll claim all spare
   horizontal space, and tighten everything around it. The default
   v-spacer steals flex-grow:1 and crushes the pills strip — so we
   hide it at this viewport and give pills-scroll the grow instead. */
@media (max-width: 480px) {
  .toolbar :deep(.v-text-field) { display: none; }
  .toolbar :deep(.v-spacer) { display: none; }
  .pills-scroll {
    flex: 1 1 0;
    /* Soft fade on the trailing edge hints there's more to scroll. */
    -webkit-mask-image:
      linear-gradient(90deg, #000 0, #000 calc(100% - 24px), transparent 100%);
            mask-image:
      linear-gradient(90deg, #000 0, #000 calc(100% - 24px), transparent 100%);
  }
  .type-pill { padding: 2px 6px; font-size: 10px; }
  /* The view-toggle is already two short labels but takes too much room
     when paired with zoom buttons on a 375px viewport. Make it tighter. */
  .toolbar :deep(.view-toggle .v-btn) { padding: 0 6px !important; min-width: 0 !important; }
}
/* Mobile bottom sheet entity panel */
.mobile-entity-sheet {
  max-height: 70vh;
  overflow-y: auto;
  background: linear-gradient(180deg, #14110a 0%, #0e0b06 100%) !important;
}
.sheet-handle {
  width: 40px;
  height: 3px;
  border-radius: 2px;
  background: rgba(200, 169, 110, 0.45);
  margin: 8px auto 0;
}
.toolbar {
  width: 100%;
  border-bottom: 1px solid rgba(200, 169, 110, 0.18);
  background: rgba(20, 17, 10, 0.7);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  backdrop-filter: blur(6px);
}
.toolbar :deep(.v-btn-toggle) {
  border: 1px solid rgba(200, 169, 110, 0.25) !important;
  border-radius: 0 !important;
}
.toolbar :deep(.v-btn) {
  font-family: Georgia, serif !important;
  font-style: italic;
  letter-spacing: 0.04em;
  color: rgba(238, 224, 196, 0.7) !important;
}
.toolbar :deep(.v-btn-toggle .v-btn--active) {
  color: #c8a96e !important;
  background: rgba(200, 169, 110, 0.1) !important;
}
.toolbar :deep(.v-text-field input) {
  font-family: Georgia, serif !important;
  font-style: italic;
  color: rgba(238, 224, 196, 0.94) !important;
}
.toolbar :deep(.v-text-field input::placeholder) {
  color: rgba(238, 224, 196, 0.45) !important;
  font-style: italic;
}
.pills-scroll {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  white-space: nowrap;
  scrollbar-width: none;
  -ms-overflow-style: none;
  flex-shrink: 1;
  min-width: 0;
}
.pills-scroll::-webkit-scrollbar { display: none; }
.status-bar {
  width: 100%;
  border-bottom: 1px solid rgba(200, 169, 110, 0.1);
  background: rgba(20, 17, 10, 0.5);
  min-height: 22px;
  font-family: Georgia, serif;
  font-style: italic;
  color: rgba(238, 224, 196, 0.55);
}
.status-bar :deep(.text-caption) {
  font-family: Georgia, serif !important;
  font-style: italic;
}
.type-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  font-family: Georgia, serif;
  font-size: 11px;
  font-style: italic;
  letter-spacing: 0.04em;
  border: 1px solid rgba(200, 169, 110, 0.18);
  background: transparent;
  color: rgba(238, 224, 196, 0.55);
  cursor: pointer;
  transition: all 150ms ease;
  flex-shrink: 0;
}
.type-pill.active {
  color: color-mix(in srgb, var(--pill-color) 75%, #c8a96e);
  border-color: color-mix(in srgb, var(--pill-color) 35%, rgba(200, 169, 110, 0.35));
  background: color-mix(in srgb, var(--pill-color) 12%, transparent);
}
.type-pill:hover {
  border-color: rgba(200, 169, 110, 0.4);
  color: rgba(238, 224, 196, 0.92);
}
.type-dot {
  width: 6px;
  height: 6px;
  background: var(--pill-color);
  opacity: 0.45;
  transition: opacity 150ms ease;
}
.type-pill.active .type-dot { opacity: 0.95; }
.view-toggle { flex-shrink: 0; }
.view-toggle :deep(.v-btn) {
  font-size: 11px !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
  min-width: 70px !important;
}
</style>
