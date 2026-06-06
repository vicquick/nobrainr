<template>
  <v-container fluid class="fill-height pa-0 d-flex flex-column">
    <!-- Toolbar — zoom/fit/refresh moved to floating cluster -->
    <div class="d-flex align-center ga-2 px-3 py-1 toolbar">
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

      <!-- Mobile-only: detached categories button — opens vertical modal -->
      <button
        v-if="viewMode === 'entities'"
        ref="catsBtnRef"
        class="cats-btn"
        :class="{ open: catsOpen }"
        @click="catsOpen = !catsOpen"
        aria-label="Filter entity categories"
        :aria-expanded="catsOpen"
      >
        <v-icon size="13">mdi-shape-outline</v-icon>
        <span class="cats-btn-label">categories</span>
        <span v-if="activeTypes.size < entityTypes.length" class="cats-btn-count">{{ activeTypes.size }}</span>
      </button>
      <v-spacer />
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        placeholder="Search..."
        clearable
        variant="outlined"
        hide-details
        style="max-width: 180px;"
        density="compact"
        class="graph-search"
      />
    </div>

    <!-- Status bar -->
    <div ref="statusBarRef" class="d-flex align-center ga-3 px-3 py-0 status-bar">
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

    <!-- Canvas + side panel -->
    <div class="graph-area" :class="{ 'panel-open': panelOpen }">
      <div ref="sigmaContainer" class="sigma-canvas" />

      <!-- Loading overlay — delayed fade-in, smooth fade-out -->
      <Transition name="graph-loader">
        <div v-if="loading" class="loading-overlay" aria-live="polite" aria-label="Loading graph">
          <div class="loading-inner">
            <span class="loading-ornament" aria-hidden="true">❦</span>
            <Dotty />
            <span class="loading-label">Tracing the graph…</span>
          </div>
        </div>
      </Transition>

      <div v-if="panelOpen" class="entity-panel">
        <GraphSidePanel :node="selectedNode" :loading="nodeLoading" :highlight="searchQuery" @close="handleClosePanel" @navigate="handleNavigateEntity" />
      </div>

      <!-- Mobile categories modal — vertical pill list, click outside closes -->
      <Transition name="cats-pop">
        <div v-if="catsOpen" ref="catsModalRef" class="cats-modal" role="menu" aria-label="Entity categories">
          <p class="cats-modal-eyebrow">— categories —</p>
          <button
            v-for="type in entityTypes"
            :key="type"
            class="type-pill cats-modal-pill"
            :class="{ active: isTypeActive(type) }"
            :style="{ '--pill-color': TYPE_COLORS[type] }"
            @click="toggleType(type)"
          >
            <span class="type-dot" />
            {{ type }}
          </button>
        </div>
      </Transition>

      <!-- Floating zoom/fit/refresh cluster — top-right, vertical, all screen sizes -->
      <div class="graph-zoom-float" aria-label="Graph view controls">
        <button class="gzf-btn" @click="refreshGraph" title="Refresh" aria-label="Refresh graph data">
          <v-icon size="14">mdi-refresh</v-icon>
        </button>
        <div class="gzf-rule" aria-hidden="true" />
        <button class="gzf-btn" @click="resetCamera" title="Fit to screen" aria-label="Fit graph to screen">
          <v-icon size="14">mdi-fit-to-screen-outline</v-icon>
        </button>
        <button class="gzf-btn" @click="zoomIn" title="Zoom in" aria-label="Zoom in">
          <v-icon size="14">mdi-plus</v-icon>
        </button>
        <button class="gzf-btn" @click="zoomOut" title="Zoom out" aria-label="Zoom out">
          <v-icon size="14">mdi-minus</v-icon>
        </button>
      </div>
    </div>

    <!-- Mobile bottom drawer — drag stripe to expand/collapse -->
    <Transition name="mob-drawer">
      <div
        v-if="mobile && selectedNode"
        class="mobile-drawer"
        :class="{ 'mobile-drawer--expanded': drawerExpanded }"
        :style="drawerStyle"
      >
        <!-- Bright stripe — gold seam at the very top edge, drag handle above the title -->
        <div
          class="drawer-stripe"
          @touchstart.passive="onDragStart"
          @touchmove.passive="onDragMove"
          @touchend="onDragEnd"
        >
          <div class="drawer-handle" aria-hidden="true" />
          <div class="drawer-titlerow">
            <span class="drawer-label">{{ selectedNode?.entity?.canonical_name }}</span>
            <button class="drawer-close-btn" @click.stop="handleClosePanel" aria-label="Close panel">✕</button>
          </div>
        </div>
        <!-- Scrollable content -->
        <div class="drawer-content">
          <GraphSidePanel
            :node="selectedNode"
            :loading="nodeLoading"
            :highlight="searchQuery"
            @close="handleClosePanel"
            @navigate="handleNavigateEntity"
          />
        </div>
      </div>
    </Transition>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDisplay } from 'vuetify'
import Sigma from 'sigma'
import Graph from 'graphology'
import { EdgeLineProgram } from 'sigma/rendering'
import { createNodeBorderProgram } from '@sigma/node-border'
import FA2Layout from 'graphology-layout-forceatlas2/worker'
import { inferSettings } from 'graphology-layout-forceatlas2'
import api from '@/api/client'
import { useGraph } from '@/composables/useGraph'
import { useSSE } from '@/composables/useSSE'
import { useChatStore } from '@/stores/chat'
import GraphSidePanel from '@/components/GraphSidePanel.vue'
import Dotty from '@/components/Dotty.vue'

// Illuminated-manuscript pigments — terracotta, gilt, ultramarine wash,
// amethyst, verdigris, madder. Muted jewel tones in one luminance band so
// no type shouts; gold stays reserved for projects (the codex accent).
const TYPE_COLORS: Record<string, string> = {
  person: '#c98a6dcc',       // terracotta
  project: '#c9a96ecc',      // gilt gold
  technology: '#7fa3c2cc',   // ultramarine wash
  concept: '#a98bc0cc',      // amethyst
  file: '#8a8f98aa',         // graphite
  config: '#a89a62cc',       // bronze-olive
  error: '#bd5a52cc',        // madder red
  location: '#74a48dcc',     // verdigris
  organization: '#6f81abcc', // indigo
}

const entityTypes = Object.keys(TYPE_COLORS)
const { mobile } = useDisplay()
const route = useRoute()
const router = useRouter()
const chatStore = useChatStore()

const { graphData, communityGraphData, selectedNode, loading, nodeLoading, fetchGraph, fetchCommunityGraph, fetchNodeDetail, clearSelection } = useGraph()

const sigmaContainer = ref<HTMLElement | null>(null)
const searchQuery = ref('')
const activeTypes = ref(new Set(entityTypes))
const nodeCount = ref(0)
const edgeCount = ref(0)
const communityCount = ref(0)
const focusedLabel = ref('')
const viewMode = ref<'communities' | 'entities'>('entities')
const drillCommunityId = ref<number | null>(null)

// Mobile drawer state
const drawerExpanded = ref(false)
const statusBarRef = ref<HTMLElement | null>(null)
// Geometry: expanded drawer top snaps to the status-bar's bottom edge —
// the toolbar numbers stay visible at every drawer position.
const drawerMaxPx = ref(0)
const drawerCollapsedPx = ref(0)
const COLLAPSED_FRACTION = 0.42

function computeDrawerMetrics() {
  const sbBottom = statusBarRef.value?.getBoundingClientRect().bottom ?? 110
  drawerMaxPx.value = Math.max(220, Math.round(window.innerHeight - sbBottom))
  drawerCollapsedPx.value = Math.min(
    Math.round(window.innerHeight * COLLAPSED_FRACTION),
    drawerMaxPx.value,
  )
}

// Mobile categories modal state
const catsOpen = ref(false)
const catsBtnRef = ref<HTMLElement | null>(null)
const catsModalRef = ref<HTMLElement | null>(null)

function onDocPointer(e: PointerEvent) {
  if (!catsOpen.value) return
  const t = e.target as Node
  if (catsModalRef.value?.contains(t) || catsBtnRef.value?.contains(t)) return
  catsOpen.value = false
}

// Live drag — drawer follows the finger 1:1; the snap happens only at
// release (and only for the remaining distance), velocity-aware.
const dragPeek = ref<number | null>(null)
let dragStartY = 0
let dragStartPeek = 0
let lastMoveY = 0
let lastMoveT = 0
let dragVelocity = 0 // px/ms, positive = downward

function collapsedPeek() {
  return Math.max(0, drawerMaxPx.value - drawerCollapsedPx.value)
}

function onDragStart(e: TouchEvent) {
  computeDrawerMetrics()
  dragStartY = e.touches[0].clientY
  dragStartPeek = drawerExpanded.value ? 0 : collapsedPeek()
  lastMoveY = dragStartY
  lastMoveT = performance.now()
  dragVelocity = 0
  dragPeek.value = dragStartPeek
}

function onDragMove(e: TouchEvent) {
  if (dragPeek.value === null) return
  const y = e.touches[0].clientY
  const now = performance.now()
  const dt = now - lastMoveT
  if (dt > 0) dragVelocity = (y - lastMoveY) / dt
  lastMoveY = y
  lastMoveT = now
  const next = dragStartPeek + (y - dragStartY)
  // Follow the finger across the full range; below-collapsed drags pull
  // toward dismissal.
  dragPeek.value = Math.min(Math.max(next, 0), drawerMaxPx.value)
}

function onDragEnd(e: TouchEvent) {
  e.preventDefault() // prevent synthetic click from toggle-firing twice
  const peek = dragPeek.value
  dragPeek.value = null // transition re-engages; only the remaining bit animates
  if (peek === null) return

  const collapsed = collapsedPeek()
  const moved = Math.abs(e.changedTouches[0].clientY - dragStartY)

  if (moved < 6) {
    drawerExpanded.value = !drawerExpanded.value // tap on stripe = toggle
    return
  }

  const FLICK = 0.45 // px/ms
  if (dragVelocity < -FLICK) { drawerExpanded.value = true; return }
  if (dragVelocity > FLICK) {
    if (peek > collapsed + 40) handleClosePanel()
    else drawerExpanded.value = false
    return
  }

  // No flick: settle to the nearest state; far below collapsed = dismiss.
  if (peek > collapsed + 80) { handleClosePanel(); return }
  drawerExpanded.value = peek < collapsed / 2
}

const drawerStyle = computed(() => {
  const style: Record<string, string> = {
    '--drawer-max': drawerMaxPx.value + 'px',
    '--drawer-peek': collapsedPeek() + 'px',
  }
  if (dragPeek.value !== null) {
    style.transform = `translateY(${dragPeek.value}px)`
    style.transition = 'none'
  }
  return style
})

// Reset drawer position when node is deselected
watch(selectedNode, (node) => {
  if (!node) drawerExpanded.value = false
})

const panelOpen = computed(() => !!selectedNode.value && !mobile.value)

let graph: Graph | null = null
let renderer: Sigma | null = null
let resizeObserver: ResizeObserver | null = null

// ── Organic settle — the Obsidian wiggle ──────────────────────────
// A short ForceAtlas2 run in a web worker right after a small graph
// mounts: nodes drift into place and breathe instead of appearing
// frozen. Sigma re-renders automatically as the worker streams new
// positions. Gated to small graphs (drill-downs, community view) —
// the full entity graph keeps its precomputed layout.
let fa2: InstanceType<typeof FA2Layout> | null = null
let fa2Timer: ReturnType<typeof setTimeout> | null = null
const SETTLE_MAX_NODES = 800
const SETTLE_DURATION_MS = 3200

function stopOrganicSettle() {
  if (fa2Timer) { clearTimeout(fa2Timer); fa2Timer = null }
  if (fa2) {
    try { fa2.kill() } catch { /* worker already gone */ }
    fa2 = null
  }
}

function startOrganicSettle() {
  stopOrganicSettle()
  if (!graph || graph.order === 0 || graph.order > SETTLE_MAX_NODES) return
  const settings = inferSettings(graph)
  fa2 = new FA2Layout(graph, {
    settings: {
      ...settings,
      slowDown: (settings.slowDown ?? 1) * 4, // gentle drift, not a jolt
      gravity: 0.5,
    },
  })
  fa2.start()
  fa2Timer = setTimeout(stopOrganicSettle, SETTLE_DURATION_MS)
}

// Custom label renderer with dark background plate — uses Georgia for consistency
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

// Hover plate — warm-white parchment plate + dark ink text, ALWAYS
// readable regardless of the node's labelColor. Sigma's default hover
// renderer draws a white plate but inherits the node's label color —
// our white labels became white-on-white (invisible).
function drawHoverWithBg(
  context: CanvasRenderingContext2D,
  data: Record<string, any>,
  settings: Record<string, any>,
): void {
  if (!data.label) return
  const size = settings.labelSize
  const font = settings.labelFont
  const weight = settings.labelWeight

  context.font = `${weight} ${size}px ${font}`
  const textWidth = context.measureText(data.label).width
  const x = data.x + data.size + 3
  const y = data.y + size / 3

  const px = 6, r = 3
  const rx = x - px, ry = y - size - 1
  const rw = textWidth + px * 2, rh = size + 7

  context.save()
  context.shadowColor = 'rgba(0, 0, 0, 0.5)'
  context.shadowBlur = 8
  context.shadowOffsetY = 2
  context.fillStyle = 'rgba(244, 238, 224, 0.97)'
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
  context.restore()

  context.fillStyle = 'rgba(20, 17, 10, 0.95)'
  context.fillText(data.label, x, y)
}

const BorderedNodeProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.12 }, color: { value: '#2e2a20' } },
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

function zoomToNodes(nodeIds: Set<string> | string[], opts: { forMobileDrawer?: boolean } = {}) {
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
  let cy = (minY + maxY) / 2
  const dx = maxX - minX
  const dy = maxY - minY
  const { width, height } = renderer.getDimensions()
  const padding = 1.5

  // On mobile with the half-drawer open, only the strip above the drawer
  // is visible. Fit the subset to that strip and shift the camera so the
  // subgraph centers in it instead of hiding behind the drawer.
  let visibleH = height
  if (opts.forMobileDrawer && mobile.value) {
    computeDrawerMetrics()
    const canvasTop = sigmaContainer.value?.getBoundingClientRect().top ?? 0
    const drawerTop = window.innerHeight - drawerCollapsedPx.value
    visibleH = Math.max(140, drawerTop - canvasTop)
  }

  const aspect = width / height
  const ratioForWidth = (dx * padding) / aspect
  const ratioForHeight = dy * padding * (height / visibleH)
  const newRatio = Math.max(ratioForWidth, ratioForHeight, 0.1)
  const clamped = Math.max(0.05, Math.min(newRatio, 2))

  if (visibleH < height) {
    // Convert the pixel offset between canvas-center and visible-center
    // into framed-graph units at the TARGET ratio (linear in ratio).
    const p1 = renderer.viewportToFramedGraph({ x: 0, y: 0 })
    const p2 = renderer.viewportToFramedGraph({ x: 0, y: 100 })
    const unitsPerPxNow = (p2.y - p1.y) / 100 // sign included
    const unitsPerPxTarget = unitsPerPxNow * (clamped / renderer.getCamera().ratio)
    const pixelShift = (height - visibleH) / 2
    cy += unitsPerPxTarget * pixelShift
  }

  renderer.getCamera().animate(
    { x: cx, y: cy, ratio: clamped },
    { duration: 400 },
  )
}

// The currently focused node set (click-focus or chat-focus), or null.
function currentFocusSet(): Set<string> | null {
  if (focusedNode) return new Set([focusedNode, ...focusedNeighbors])
  if (chatFocusedNodes.size > 0) return new Set(chatFocusedNodes)
  return null
}

// Keep the focused subset fitted + centered in the canvas extent of the
// moment: the desktop side panel shrinks the canvas (250ms padding
// transition), window resizes and rotations change it — every time the
// canvas settles, the subset re-fits.
let refitTimer: ReturnType<typeof setTimeout> | null = null
function scheduleRefit() {
  const set = currentFocusSet()
  if (!set) return
  if (refitTimer) clearTimeout(refitTimer)
  refitTimer = setTimeout(() => {
    refitTimer = null
    const current = currentFocusSet()
    if (current) {
      zoomToNodes(current, { forMobileDrawer: mobile.value && !!selectedNode.value })
    }
  }, 140)
}

function focusNode(nodeId: string) {
  focusedNode = nodeId
  focusedNeighbors.clear()
  chatFocusedNodes.clear()
  chatFocusedNeighbors.clear()
  graph!.forEachNeighbor(nodeId, (n) => focusedNeighbors.add(n))
  focusedLabel.value = graph!.getNodeAttribute(nodeId, 'label') || ''
  renderer?.refresh()
  // On mobile the half-drawer opens with the selection — fit to the
  // remaining visible strip above it.
  zoomToNodes(new Set([nodeId, ...focusedNeighbors]), { forMobileDrawer: mobile.value })
}

function unfocusNode() {
  focusedNode = null
  focusedNeighbors.clear()
  focusedLabel.value = ''
  renderer?.refresh()
}

function initSigma() {
  if (!sigmaContainer.value || !graphData.value) return

  stopOrganicSettle()
  if (renderer) {
    renderer.kill()
    renderer = null
  }

  chatFocusedNodes.clear()
  chatFocusedNeighbors.clear()
  focusedNode = null
  focusedNeighbors.clear()
  focusedLabel.value = ''

  graph = new Graph()

  const communities = new Set<number>()

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
      color: TYPE_COLORS[node.data.type] || '#8a8f98aa',
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
          color: '#272218',
        })
      } catch {
        // duplicate edge
      }
    }
  }

  nodeCount.value = graph.order
  edgeCount.value = graph.size
  communityCount.value = communities.size

  hubNodes.clear()
  graph.forEachNode((node) => {
    if (graph!.degree(node) >= 10) hubNodes.add(node)
  })

  renderer = new Sigma(graph, sigmaContainer.value, {
    defaultEdgeType: 'line',
    edgeProgramClasses: { line: EdgeLineProgram },
    enableEdgeEvents: false,
    // Sigma v3 keys — the old `drawLabel` key was silently ignored, so the
    // custom plate renderer never ran: focused labels rendered #000-on-dark
    // (invisible) and hover used sigma's default white-plate + white text.
    defaultDrawNodeLabel: drawLabelWithBg,
    defaultDrawNodeHover: drawHoverWithBg,
    renderLabels: true,
    labelColor: { attribute: 'labelColor', color: 'rgba(255, 255, 255, 0.7)' },
    labelSize: 11,
    labelFont: 'Georgia, "Palatino Linotype", Palatino, serif',
    labelWeight: '500',
    labelDensity: 0.12,
    labelGridCellSize: 100,
    labelRenderedSizeThreshold: 5,
    defaultNodeColor: '#8a8f98aa',
    defaultEdgeColor: '#272218',
    stagePadding: 40,
    zIndex: true,
    hideEdgesOnMove: true,

    nodeReducer(node, data) {
      const res = { ...data }
      const type = graph!.getNodeAttribute(node, 'nodeType')

      if (activeTypes.value.size < entityTypes.length && !activeTypes.value.has(type)) {
        res.hidden = true
        return res
      }

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
          // Unrelated nodes vanish entirely — the focused subgraph gets
          // the whole stage (and the renderer skips thousands of quads).
          res.hidden = true
        }
        return res
      }

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
          res.hidden = true
        }
        return res
      }

      if (searchMatches.size > 0) {
        if (searchMatches.has(node)) {
          res.zIndex = 1
          res.color = lighten(res.color as string, 0.3)
          res.forceLabel = true
          res.labelColor = 'rgba(255, 255, 255, 0.9)'
        } else {
          res.color = 'rgba(72, 65, 52, 0.15)'
          res.size = 1.5
          res.label = ''
        }
        return res
      }

      if (hoveredNode === node) {
        res.forceLabel = true
        res.labelColor = '#000000'
        res.labelBgColor = 'rgba(255, 255, 255, 0.92)'
      }

      return res
    },

    edgeReducer(edge, data) {
      const res = { ...data }

      if (focusedNode) {
        if (graph!.extremities(edge).includes(focusedNode)) {
          res.color = '#8a7a5a'
          res.size = 1.5
          res.zIndex = 1
        } else {
          res.hidden = true
        }
        return res
      }

      if (chatFocusedNodes.size > 0) {
        const [src, tgt] = graph!.extremities(edge)
        if (chatFocusedNodes.has(src) || chatFocusedNodes.has(tgt)) {
          res.color = '#8a7a5a'
          res.size = 1.5
          res.zIndex = 1
        } else {
          res.hidden = true
        }
        return res
      }

      if (searchMatches.size > 0) {
        const [src, tgt] = graph!.extremities(edge)
        if (!searchMatches.has(src) || !searchMatches.has(tgt)) {
          res.hidden = true
        } else {
          res.color = '#4d4430'
        }
        return res
      }

      if (hoveredNode) {
        const [src, tgt] = graph!.extremities(edge)
        if (src === hoveredNode || tgt === hoveredNode) {
          res.color = '#8a7a5a'
          res.size = 1.5
          res.zIndex = 1
          return res
        }
      }

      const [src, tgt] = graph!.extremities(edge)
      if (!hubNodes.has(src) || !hubNodes.has(tgt)) {
        res.hidden = true
      }

      return res
    },
  })

  // Entity-view hover only restyles the hovered node + its edges, so a
  // partial refresh skips re-running reducers across thousands of nodes —
  // this is where most of the hover stutter came from.
  renderer.on('enterNode', ({ node }) => {
    hoveredNode = node
    hoveredNeighbors.clear()
    if (graph) graph.forEachNeighbor(node, (n) => hoveredNeighbors.add(n))
    sigmaContainer.value!.style.cursor = 'pointer'
    renderer?.refresh({
      partialGraph: { nodes: [node], edges: graph?.edges(node) ?? [] },
      skipIndexation: true,
    })
  })
  renderer.on('leaveNode', () => {
    const prev = hoveredNode
    hoveredNode = null
    hoveredNeighbors.clear()
    sigmaContainer.value!.style.cursor = 'default'
    if (prev && graph?.hasNode(prev)) {
      renderer?.refresh({
        partialGraph: { nodes: [prev], edges: graph.edges(prev) },
        skipIndexation: true,
      })
    } else {
      renderer?.refresh()
    }
  })

  renderer.on('clickNode', async ({ node }) => {
    focusNode(node)
    await fetchNodeDetail(node)
  })

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

  stopOrganicSettle()
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
          color: '#28231a',
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
    defaultDrawNodeLabel: drawLabelWithBg,
    defaultDrawNodeHover: drawHoverWithBg,
    renderLabels: true,
    labelColor: { attribute: 'labelColor', color: 'rgba(255, 255, 255, 0.85)' },
    labelSize: 13,
    labelFont: 'Georgia, "Palatino Linotype", Palatino, serif',
    labelWeight: '600',
    labelDensity: 0.5,
    labelGridCellSize: 120,
    labelRenderedSizeThreshold: 4,
    defaultNodeColor: '#8a8f98',
    defaultEdgeColor: '#28231a',
    stagePadding: 60,
    zIndex: true,

    nodeReducer(node, data) {
      const res = { ...data }
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
          res.color = 'rgba(72, 65, 52, 0.30)'
          res.label = ''
        }
      }
      if (searchMatches.size > 0) {
        if (searchMatches.has(node)) {
          res.forceLabel = true
          res.zIndex = 1
        } else {
          res.color = 'rgba(72, 65, 52, 0.15)'
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
  await fetchGraph()
  await nextTick()
  requestAnimationFrame(() => {
    initSigma()
    startOrganicSettle() // drill subsets are small — let them breathe in
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
    startOrganicSettle()
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

// Search: instant local substring pass for immediate feedback, then a
// debounced hybrid call (trigram + pgvector semantic, RRF-fused) merges
// in conceptually related entities that share no keyword with the query.
let searchTimeout: ReturnType<typeof setTimeout>
let searchSeq = 0
watch(searchQuery, (q) => {
  clearTimeout(searchTimeout)
  const seq = ++searchSeq
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
  if (!q || q.trim().length < 2) return

  searchTimeout = setTimeout(async () => {
    try {
      const { data } = await api.get('/api/graph/search', { params: { q: q.trim() } })
      if (seq !== searchSeq) return // a newer query superseded this one
      let added = false
      for (const hit of data.results ?? []) {
        if (graph?.hasNode(hit.id) && !searchMatches.has(hit.id)) {
          searchMatches.add(hit.id)
          added = true
        }
      }
      if (added) renderer?.refresh()
    } catch { /* network hiccup — local matches already shown */ }
  }, 280)
})

async function handleNavigateEntity(entityId: string) {
  if (!entityId || !graph) return
  if (graph.hasNode(entityId)) {
    focusNode(entityId)
  }
  await fetchNodeDetail(entityId)
}

watch(() => chatStore.focusEntityId, async (entityId) => {
  if (!entityId || !graph || !graph.hasNode(entityId)) return
  focusNode(entityId)
  await fetchNodeDetail(entityId)
  chatStore.clearFocus()
})

watch(() => chatStore.currentSources, async (sources) => {
  if (!sources || !graph) return

  focusedNode = null
  focusedNeighbors.clear()
  clearSelection()

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

  chatFocusedNeighbors.clear()
  for (const nodeId of chatFocusedNodes) {
    if (!graph.hasNode(nodeId)) continue
    graph.forEachNeighbor(nodeId, (n) => {
      if (!chatFocusedNodes.has(n)) chatFocusedNeighbors.add(n)
    })
  }

  focusedLabel.value = chatFocusedNodes.size > 0
    ? `${chatFocusedNodes.size} chat entities`
    : ''

  if (chatFocusedNodes.size > 0) {
    renderer?.refresh()
    if (added) zoomToNodes(chatFocusedNodes)
    if (firstEntityId) {
      await fetchNodeDetail(firstEntityId)
    }
  } else {
    renderer?.refresh()
  }
})

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

watch(viewMode, async (mode) => {
  if (mode === 'communities' && drillCommunityId.value === null) {
    await switchToCommunityView()
  } else if (mode === 'entities' && drillCommunityId.value === null) {
    await switchToEntityView()
  }
})

const graphStale = ref(false)
useSSE((evt) => {
  // Only entity-graph changes matter here. memory_created fires on every
  // write (session logs, scheduler jobs) and cried wolf constantly.
  if (evt.type === 'entities_changed') {
    graphStale.value = true
  }
})

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
  // One RAF before heavy init so the loading overlay renders its first animation frame
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  if (viewMode.value === 'communities') {
    initCommunitySigma()
  } else {
    initSigma()
  }
  startOrganicSettle()
  loading.value = false

  if (sigmaContainer.value) {
    resizeObserver = new ResizeObserver(() => {
      renderer?.resize()
      renderer?.refresh()
      // Canvas extent changed (side panel open/close, window resize,
      // rotation) — re-fit the focused subset to the new extent.
      scheduleRefit()
    })
    resizeObserver.observe(sigmaContainer.value)
  }

  computeDrawerMetrics()
  window.addEventListener('resize', computeDrawerMetrics)
  document.addEventListener('pointerdown', onDocPointer)

  await maybeApplyFocusFromQuery()
})

async function maybeApplyFocusFromQuery() {
  const focusId = typeof route.query.focus === 'string' ? route.query.focus : null
  if (!focusId) return
  if (graph?.hasNode(focusId)) {
    focusNode(focusId)
    await fetchNodeDetail(focusId)
  } else {
    await fetchNodeDetail(focusId)
  }
  router.replace({ path: '/graph', query: {} })
}

onUnmounted(() => {
  stopOrganicSettle()
  if (refitTimer) clearTimeout(refitTimer)
  renderer?.kill()
  resizeObserver?.disconnect()
  window.removeEventListener('resize', computeDrawerMetrics)
  document.removeEventListener('pointerdown', onDocPointer)
})
</script>

<style scoped>
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

/* ── Loading overlay — delayed entry, smooth exit ──────────────── */
.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(14, 11, 6, 0.90);
  z-index: 10;
  pointer-events: none;
}
.loading-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.loading-ornament {
  font-size: 34px;
  color: rgba(200, 169, 110, 0.55);
  line-height: 1;
  animation: ornament-breathe 2.8s ease-in-out infinite;
}
@keyframes ornament-breathe {
  0%, 100% { opacity: 0.35; transform: scale(1); }
  50%       { opacity: 0.75; transform: scale(1.12); }
}
.loading-label {
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: rgba(238, 224, 196, 0.38);
}

/* Delayed enter — only appears if loading takes >100ms; exits smoothly */
.graph-loader-enter-active {
  transition: opacity 350ms ease;
  transition-delay: 100ms;
}
.graph-loader-enter-from { opacity: 0; }
.graph-loader-leave-active {
  transition: opacity 600ms cubic-bezier(0.4, 0, 0.2, 1);
}
.graph-loader-leave-to { opacity: 0; }

/* ── Entity side panel ─────────────────────────────────────────── */
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

/* ── Floating zoom cluster ─────────────────────────────────────── */
.graph-zoom-float {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 8;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  background: rgba(14, 11, 6, 0.80);
  border: 1px solid rgba(200, 169, 110, 0.20);
  border-radius: 4px;
  padding: 3px;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
}
.gzf-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: 2px;
  cursor: pointer;
  color: rgba(238, 224, 196, 0.50);
  transition:
    color 150ms ease,
    background 150ms ease;
  padding: 0;
}
.gzf-btn:hover {
  color: rgba(200, 169, 110, 0.90);
  background: rgba(200, 169, 110, 0.08);
}
.gzf-btn:focus-visible {
  outline: none;
  box-shadow: var(--cp-focus-ring);
}
.gzf-rule {
  width: 16px;
  height: 1px;
  background: rgba(200, 169, 110, 0.14);
  margin: 2px 0;
  flex-shrink: 0;
}

/* ── Mobile drawer ─────────────────────────────────────────────── */
.mobile-drawer {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  /* Height = viewport minus status-bar bottom, computed in JS.
     Collapsed: transform reveals only the bottom 42vh.
     Expanded: translateY(0) → top edge snaps exactly to the
     status-bar's bottom edge; the counts stay readable. */
  height: var(--drawer-max, 80vh);
  transform: translateY(var(--drawer-peek, 38vh));
  transition: transform 340ms cubic-bezier(0, 0, 0.2, 1);
  z-index: 50;
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, #14110a 0%, #0e0b06 100%);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.mobile-drawer--expanded {
  transform: translateY(0);
}

/* Transition: slide up from fully below viewport */
.mob-drawer-enter-active {
  transition: transform 360ms cubic-bezier(0, 0, 0.2, 1);
}
.mob-drawer-leave-active {
  transition: transform 260ms cubic-bezier(0.4, 0, 1, 1);
}
.mob-drawer-enter-from,
.mob-drawer-leave-to {
  transform: translateY(100%) !important;
}

/* ── Bright stripe — gold seam at the drawer's top edge ────────── */
.drawer-stripe {
  flex-shrink: 0;
  border-top: 2px solid rgba(200, 169, 110, 0.48);
  background: linear-gradient(
    180deg,
    rgba(200, 169, 110, 0.12) 0%,
    rgba(14, 11, 6, 0.0) 100%
  );
  display: flex;
  flex-direction: column;
  align-items: stretch;
  padding: 0 14px 6px;
  cursor: ns-resize;
  touch-action: none;
  user-select: none;
}
/* Drag handle — centered, at the very top, above the title */
.drawer-handle {
  width: 44px;
  height: 3px;
  border-radius: 2px;
  background: rgba(200, 169, 110, 0.55);
  margin: 6px auto 6px;
  flex-shrink: 0;
}
.drawer-titlerow {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.drawer-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13.5px;
  color: rgba(238, 224, 196, 0.72);
  letter-spacing: 0.02em;
  pointer-events: none;
}
.drawer-close-btn {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 3px;
  cursor: pointer;
  color: rgba(238, 224, 196, 0.40);
  font-size: 11px;
  font-family: Georgia, serif;
  transition: color 150ms ease, border-color 150ms ease;
  padding: 0;
  line-height: 1;
}
.drawer-close-btn:hover {
  color: rgba(238, 224, 196, 0.85);
  border-color: rgba(200, 169, 110, 0.25);
}
.drawer-content {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}

/* ── Toolbar ───────────────────────────────────────────────────── */
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
  background: transparent !important; /* buttons carry their own fill */
}
.toolbar :deep(.v-btn-toggle .v-btn) {
  background: transparent;
}
/* Search — same look as the commonplace bar: theme-default outline +
   icon (ink-toned), no grey overlay fill. The earlier gold currentColor
   override washed out frame AND icon — commonplace never overrides
   these, which is exactly why it looks right. */
.toolbar :deep(.v-field__overlay) {
  display: none;
}
.toolbar :deep(.graph-search .v-field) {
  border-radius: 2px;
}
.toolbar :deep(.v-btn),
.toolbar :deep(.v-btn__content) {
  font-family: Georgia, serif !important;
  font-style: italic;
  letter-spacing: 0.04em;
  color: rgba(238, 224, 196, 0.7) !important;
}
.toolbar :deep(.v-btn-toggle .v-btn--active),
.toolbar :deep(.v-btn-toggle .v-btn--active .v-btn__content) {
  color: #c8a96e !important;
  background: rgba(200, 169, 110, 0.1) !important;
}
.toolbar :deep(.v-text-field input),
.toolbar :deep(.v-field__input) {
  font-family: Georgia, serif !important;
  font-style: italic;
  color: rgba(238, 224, 196, 0.94) !important;
}
.toolbar :deep(.v-text-field input::placeholder),
.toolbar :deep(.v-field__input::placeholder) {
  color: rgba(238, 224, 196, 0.45) !important;
  font-style: italic;
}

/* ── Status bar ────────────────────────────────────────────────── */
.status-bar {
  width: 100%;
  border-bottom: 1px solid rgba(200, 169, 110, 0.1);
  background: rgba(20, 17, 10, 0.5);
  min-height: 22px;
  font-family: Georgia, serif;
  font-style: italic;
  color: rgba(238, 224, 196, 0.55);
}
.status-bar :deep(.text-caption),
.status-bar :deep(*) {
  font-family: Georgia, serif !important;
  font-style: italic;
}

/* ── Entity type pills ─────────────────────────────────────────── */
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

/* ── Mobile categories button + vertical modal ─────────────────── */
/* Hidden on desktop — pills row covers it. Shown <=480px. */
.cats-btn {
  display: none;
  align-items: center;
  gap: 5px;
  margin-left: 10px; /* slightly detached, nudged right of the toggles */
  padding: 3px 9px;
  font-family: Georgia, serif;
  font-size: 11px;
  font-style: italic;
  letter-spacing: 0.05em;
  border: 1px solid rgba(200, 169, 110, 0.28);
  background: transparent;
  color: rgba(238, 224, 196, 0.65);
  cursor: pointer;
  transition: all 150ms ease;
  flex-shrink: 0;
}
.cats-btn.open,
.cats-btn:hover {
  color: #c8a96e;
  border-color: rgba(200, 169, 110, 0.5);
  background: rgba(200, 169, 110, 0.08);
}
.cats-btn-count {
  font-size: 9.5px;
  font-style: normal;
  font-variant-numeric: tabular-nums;
  color: #c8a96e;
  border: 1px solid rgba(200, 169, 110, 0.35);
  border-radius: 8px;
  padding: 0 5px;
  line-height: 1.4;
}

.cats-modal {
  position: absolute;
  top: 10px;
  left: 10px;
  z-index: 9;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px;
  background: rgba(14, 11, 6, 0.92);
  border: 1px solid rgba(200, 169, 110, 0.25);
  border-radius: 4px;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.55);
  max-height: calc(100% - 24px);
  overflow-y: auto;
}
.cats-modal-eyebrow {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: rgba(200, 169, 110, 0.5);
  margin: 0 0 4px;
  text-align: center;
}
.cats-modal-pill {
  justify-content: flex-start;
  width: 100%;
}

.cats-pop-enter-active {
  transition: opacity 180ms cubic-bezier(0, 0, 0.2, 1), transform 180ms cubic-bezier(0, 0, 0.2, 1);
}
.cats-pop-leave-active {
  transition: opacity 130ms cubic-bezier(0.4, 0, 1, 1), transform 130ms cubic-bezier(0.4, 0, 1, 1);
}
.cats-pop-enter-from,
.cats-pop-leave-to {
  opacity: 0;
  transform: translateY(-6px) scale(0.97);
}

/* ── View toggle ───────────────────────────────────────────────── */
.view-toggle { flex-shrink: 0; }
.view-toggle :deep(.v-btn) {
  font-size: 11px !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
  min-width: 70px !important;
  font-family: Georgia, serif !important;
  font-style: italic !important;
}

/* ── Responsive ────────────────────────────────────────────────── */
@media (min-width: 600px) and (max-width: 960px) {
  .graph-area { --panel-width: 340px; }
}
@media (max-width: 960px) {
  .graph-area { transition: none; }
}
/* Mobile: pills row replaced by the detached categories button +
   vertical modal. Search + spacer hidden to keep the bar tight. */
@media (max-width: 480px) {
  .toolbar :deep(.v-text-field) { display: none; }
  .toolbar :deep(.v-spacer) { display: none; }
  .pills-scroll { display: none; }
  .cats-btn { display: inline-flex; }
  .toolbar :deep(.view-toggle .v-btn) { padding: 0 6px !important; min-width: 0 !important; }
}
</style>
