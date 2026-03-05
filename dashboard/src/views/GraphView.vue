<template>
  <v-container fluid class="fill-height pa-0 d-flex flex-column">
    <!-- Toolbar -->
    <div class="d-flex align-center ga-3 pa-3" style="border-bottom: 1px solid rgba(255,255,255,0.1);">
      <v-select
        v-model="typeFilter"
        :items="typeOptions"
        label="Entity types"
        variant="outlined"
        density="compact"
        multiple
        chips
        closable-chips
        clearable
        hide-details
        style="max-width: 400px;"
      />
      <v-text-field
        v-model="searchQuery"
        prepend-inner-icon="mdi-magnify"
        placeholder="Search nodes..."
        variant="outlined"
        density="compact"
        clearable
        hide-details
        style="max-width: 250px;"
      />
      <v-btn icon="mdi-refresh" variant="text" @click="resetView" />
    </div>

    <!-- Loading -->
    <div v-if="loading" class="d-flex align-center justify-center flex-grow-1">
      <v-progress-circular indeterminate color="primary" size="48" />
    </div>

    <!-- Cytoscape Canvas -->
    <div v-show="!loading" ref="cyContainer" class="flex-grow-1" style="width: 100%;" />

    <!-- Side Panel -->
    <GraphSidePanel :node="selectedNode" @close="clearSelection" />
  </v-container>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue'
import cytoscape from 'cytoscape'
import fcose from 'cytoscape-fcose'
import { useGraph } from '@/composables/useGraph'
import { useSSE } from '@/composables/useSSE'
import GraphSidePanel from '@/components/GraphSidePanel.vue'

cytoscape.use(fcose)

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

const typeOptions = Object.keys(TYPE_COLORS)

const { graphData, selectedNode, loading, fetchGraph, fetchNodeDetail, clearSelection } = useGraph()

const cyContainer = ref<HTMLElement | null>(null)
const searchQuery = ref('')
const typeFilter = ref<string[]>([])

let cy: cytoscape.Core | null = null

function initCytoscape() {
  if (!cyContainer.value || !graphData.value) return
  if (cy) cy.destroy()

  cy = cytoscape({
    container: cyContainer.value,
    elements: [
      ...graphData.value.nodes.map((n) => ({ group: 'nodes' as const, data: n.data })),
      ...graphData.value.edges.map((e) => ({ group: 'edges' as const, data: e.data })),
    ],
    style: [
      {
        selector: 'node',
        style: {
          'background-color': (ele: cytoscape.NodeSingular) =>
            TYPE_COLORS[ele.data('type')] || '#8b949e',
          label: 'data(label)',
          width: (ele: cytoscape.NodeSingular) =>
            Math.max(20, Math.min(60, (ele.data('mention_count') || 1) * 3)),
          height: (ele: cytoscape.NodeSingular) =>
            Math.max(20, Math.min(60, (ele.data('mention_count') || 1) * 3)),
          'font-size': '10px',
          color: '#ffffff',
          'text-outline-color': '#0d1117',
          'text-outline-width': 2,
          'text-valign': 'bottom',
          'text-margin-y': 5,
        } as cytoscape.Css.Node,
      },
      {
        selector: 'edge',
        style: {
          'line-color': '#30363d',
          width: (ele: cytoscape.EdgeSingular) =>
            Math.max(1, Math.min(5, (ele.data('confidence') || 0.5) * 5)),
          'curve-style': 'bezier',
          'target-arrow-shape': 'triangle',
          'target-arrow-color': '#30363d',
          label: 'data(label)',
          'font-size': '8px',
          color: '#8b949e',
          'text-rotation': 'autorotate',
          'text-outline-color': '#0d1117',
          'text-outline-width': 1,
        } as cytoscape.Css.Edge,
      },
      {
        selector: '.dimmed',
        style: { opacity: 0.15 },
      },
      {
        selector: '.highlighted',
        style: { opacity: 1 },
      },
    ],
    layout: {
      name: 'fcose',
      quality: 'default',
      nodeRepulsion: 8000,
      idealEdgeLength: 120,
      gravity: 0.25,
      numIter: 2500,
      animate: false,
    } as cytoscape.LayoutOptions,
    minZoom: 0.1,
    maxZoom: 5,
  })

  // Node tap
  cy.on('tap', 'node', async (evt) => {
    const node = evt.target
    const id = node.data('id')

    // Highlight
    cy!.elements().addClass('dimmed')
    node.removeClass('dimmed').addClass('highlighted')
    node.connectedEdges().removeClass('dimmed').addClass('highlighted')
    node.neighborhood('node').removeClass('dimmed').addClass('highlighted')

    await fetchNodeDetail(id)
  })

  // Background tap
  cy.on('tap', (evt) => {
    if (evt.target === cy) {
      cy!.elements().removeClass('dimmed highlighted')
      clearSelection()
    }
  })
}

function resetView() {
  if (!cy) return
  cy.elements().removeClass('dimmed highlighted')
  clearSelection()
  searchQuery.value = ''
  typeFilter.value = []
  cy.elements().show()
  cy.fit()
}

// Search filter
watch(searchQuery, (q) => {
  if (!cy) return
  if (!q) {
    cy.elements().removeClass('dimmed highlighted')
    return
  }
  const lower = q.toLowerCase()
  cy.elements().addClass('dimmed')
  cy.nodes()
    .filter((n) => n.data('label')?.toLowerCase().includes(lower))
    .removeClass('dimmed')
    .addClass('highlighted')
})

// Type filter
watch(typeFilter, (types) => {
  if (!cy) return
  if (!types.length) {
    cy.nodes().show()
    cy.edges().show()
    return
  }
  cy.nodes().forEach((n) => {
    if (types.includes(n.data('type'))) {
      n.show()
    } else {
      n.hide()
    }
  })
  cy.edges().forEach((e) => {
    const srcVisible = e.source().visible()
    const tgtVisible = e.target().visible()
    if (srcVisible && tgtVisible) {
      e.show()
    } else {
      e.hide()
    }
  })
})

useSSE(async (evt) => {
  if (['memory_created', 'memory_deleted'].includes(evt.type)) {
    await fetchGraph()
    initCytoscape()
  }
})

onMounted(async () => {
  await fetchGraph()
  initCytoscape()
})

onUnmounted(() => {
  if (cy) {
    cy.destroy()
    cy = null
  }
})
</script>
