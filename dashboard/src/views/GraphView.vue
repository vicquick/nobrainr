<template>
  <v-container fluid class="fill-height pa-0 d-flex flex-column">
    <!-- Compact toolbar -->
    <div class="d-flex align-center ga-2 px-3 py-1 toolbar">
      <div class="pills-scroll">
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
        <v-btn icon="mdi-minus" variant="text" size="x-small" @click="zoomOut" />
        <v-btn icon="mdi-plus" variant="text" size="x-small" @click="zoomIn" />
        <v-btn icon="mdi-fit-to-screen-outline" variant="text" size="x-small" @click="resetCamera" />
        <v-btn icon="mdi-refresh" variant="text" size="x-small" @click="refreshGraph" />
      </div>
    </div>

    <!-- Status bar -->
    <div class="d-flex align-center ga-3 px-3 py-0 status-bar">
      <span class="text-caption text-medium-emphasis" style="font-variant-numeric: tabular-nums; font-size: 10px;">
        {{ nodeCount.toLocaleString() }} nodes · {{ edgeCount.toLocaleString() }} edges · {{ communityCount }} clusters
      </span>
      <v-spacer />
      <span v-if="focusedLabel" class="text-caption" style="font-size: 10px;">
        <span class="text-medium-emphasis">focused:</span>
        <span class="ml-1 font-weight-medium">{{ focusedLabel }}</span>
      </span>
    </div>

    <!-- Canvas + Entity Side Panel -->
    <div class="graph-area" :style="panelOpen ? { paddingRight: '420px' } : {}">
      <div ref="sigmaContainer" class="sigma-canvas" />
      <!-- Loading overlay (on top of canvas so container always has dimensions) -->
      <div v-if="loading" class="loading-overlay">
        <div class="text-center">
          <v-progress-circular indeterminate color="primary" size="40" width="2" class="mb-3" />
          <div class="text-caption text-medium-emphasis">Loading graph...</div>
        </div>
      </div>
      <div v-if="panelOpen" class="entity-panel">
        <GraphSidePanel :node="selectedNode" @close="handleClosePanel" />
      </div>
    </div>

    <!-- Mobile overlay for entity panel -->
    <v-dialog v-if="mobile && !!selectedNode" v-model="showMobilePanel" fullscreen transition="dialog-right-transition">
      <v-card color="#12121a">
        <GraphSidePanel :node="selectedNode" @close="handleClosePanel" />
      </v-card>
    </v-dialog>
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
  person: '#7b8ec8',
  project: '#6ba87a',
  technology: '#9585c4',
  concept: '#c4a46a',
  file: '#7a8290',
  config: '#b09060',
  error: '#c46b6b',
  location: '#6b9e8f',
  organization: '#7d92b0',
}

const entityTypes = Object.keys(TYPE_COLORS)
const { mobile } = useDisplay()
const chatStore = useChatStore()

const { graphData, selectedNode, loading, fetchGraph, fetchNodeDetail, clearSelection } = useGraph()

const sigmaContainer = ref<HTMLElement | null>(null)
const searchQuery = ref('')
const activeTypes = ref(new Set(entityTypes))
const nodeCount = ref(0)
const edgeCount = ref(0)
const communityCount = ref(0)
const focusedLabel = ref('')

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
  const color = data.labelColor || 'rgba(255, 255, 255, 0.7)'

  context.font = `${weight} ${size}px ${font}`
  const textWidth = context.measureText(data.label).width
  const x = data.x + data.size + 3
  const y = data.y + size / 3

  // Background plate — tight to text
  const px = 4, r = 3
  const rx = x - px, ry = y - size + 1
  const rw = textWidth + px * 2, rh = size + 3
  context.fillStyle = 'rgba(10, 10, 14, 0.8)'
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

  for (const node of graphData.value.nodes) {
    const mc = node.data.mention_count || 1
    communities.add(node.data.community)
    graph.addNode(node.data.id, {
      label: node.data.label,
      x: node.data.x,
      y: node.data.y,
      size: Math.max(3, Math.min(18, Math.sqrt(mc) * 2.8)),
      color: TYPE_COLORS[node.data.type] || '#6b7280',
      labelColor: 'rgba(255, 255, 255, 0.7)',
      nodeType: node.data.type,
      community: node.data.community,
    })
  }

  for (const edge of graphData.value.edges) {
    if (graph.hasNode(edge.data.source) && graph.hasNode(edge.data.target)) {
      try {
        graph.addEdge(edge.data.source, edge.data.target, {
          label: edge.data.label,
          size: 1,
          color: 'rgba(255, 255, 255, 0.008)',
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
    // Node rendering — bordered circles for depth
    defaultNodeType: 'bordered',
    nodeProgramClasses: { bordered: BorderedNodeProgram },

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
    labelDensity: 0.07,
    labelGridCellSize: 80,
    labelRenderedSizeThreshold: 8,

    // Defaults
    defaultNodeColor: '#6b7280',
    defaultEdgeColor: 'rgba(255, 255, 255, 0.008)',
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
          res.size = (res.size as number) * 1.4
          res.forceLabel = true
          res.labelColor = '#ffffff'
        } else if (focusedNeighbors.has(node)) {
          res.zIndex = 1
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.85)'
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
          res.size = (res.size as number) * 1.4
          res.forceLabel = true
          res.labelColor = '#ffffff'
        } else if (chatFocusedNeighbors.has(node)) {
          res.zIndex = 1
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.85)'
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

      // Hover: show label
      if (hoveredNode === node) {
        res.forceLabel = true
        res.labelColor = '#ffffff'
      }

      return res
    },

    edgeReducer(edge, data) {
      const res = { ...data }

      // Click-focus: show only edges to focused node
      if (focusedNode) {
        if (graph!.extremities(edge).includes(focusedNode)) {
          res.color = 'rgba(255, 255, 255, 0.15)'
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
          res.color = 'rgba(255, 255, 255, 0.15)'
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
          res.color = 'rgba(255, 255, 255, 0.1)'
        }
        return res
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
    sigmaContainer.value!.style.cursor = 'pointer'
    renderer?.refresh()
  })
  renderer.on('leaveNode', () => {
    hoveredNode = null
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
  unfocusNode()
  clearSelection()
  await fetchGraph()
  await nextTick()
  requestAnimationFrame(() => {
    initSigma()
    loading.value = false
  })
}

useSSE(async (evt) => {
  if (['memory_created', 'memory_deleted'].includes(evt.type)) {
    await fetchGraph()
    await nextTick()
    requestAnimationFrame(() => {
      initSigma()
      loading.value = false
    })
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
  await fetchGraph()
  await nextTick()
  await waitForLayout()
  initSigma()
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
.graph-area {
  flex: 1;
  width: 100%;
  position: relative;
  min-height: 0;
  box-sizing: border-box;
  transition: padding-right 250ms ease;
  overflow: hidden;
}
.sigma-canvas {
  width: 100%;
  height: 100%;
  background: #101016;
}
.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #101016;
  z-index: 10;
}
.entity-panel {
  position: absolute;
  right: 0;
  top: 0;
  bottom: 0;
  width: 420px;
  background: #12121a;
  border-left: 1px solid rgba(255, 255, 255, 0.08);
  overflow-y: auto;
  z-index: 5;
}
@media (max-width: 960px) {
  .graph-area { transition: none; }
}
.toolbar {
  width: 100%;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  background: rgba(16, 16, 22, 0.6);
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
.pills-scroll::-webkit-scrollbar {
  display: none;
}
.status-bar {
  width: 100%;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  background: rgba(16, 16, 22, 0.4);
  min-height: 20px;
}
.type-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.01em;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.02);
  color: rgba(255, 255, 255, 0.35);
  cursor: pointer;
  transition: all 150ms ease;
  font-family: inherit;
  flex-shrink: 0;
}
.type-pill.active {
  color: color-mix(in srgb, var(--pill-color) 85%, white);
  border-color: color-mix(in srgb, var(--pill-color) 25%, transparent);
  background: color-mix(in srgb, var(--pill-color) 10%, transparent);
}
.type-pill:hover {
  border-color: rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.04);
}
.type-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  background: var(--pill-color);
  opacity: 0.25;
  transition: opacity 150ms ease;
}
.type-pill.active .type-dot {
  opacity: 0.9;
}
</style>
