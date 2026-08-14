<template>
  <div class="constellarium" :class="{ ready }">
    <div ref="canvasHost" class="cosmos-host" />

    <!-- Plate: title + living stats -->
    <header class="plate">
      <p class="plate-kicker">CONSTELLARIUM · THE LIVING GRAPH</p>
      <h1 class="plate-title">Every entity, in motion</h1>
      <p class="plate-stats" v-if="ready">
        <em>{{ nodeCount.toLocaleString() }}</em> stars ·
        <em>{{ linkCount.toLocaleString() }}</em> threads
        <span class="sep">·</span>
        <button class="plate-act" @click="stir">stir the sky</button>
        <span class="sep">·</span>
        <button class="plate-act" @click="refit">re-center</button>
        <span class="sep">·</span>
        <span class="tier-picker">
          <button
            v-for="(t, i) in TIERS" :key="t.label"
            class="plate-act tier-opt" :class="{ active: i === tier }"
            @click="setTier(i)"
          >{{ t.label }}</button>
        </span>
      </p>
      <p class="plate-stats" v-else>charting the heavens…</p>
    </header>

    <!-- Seek: focus a named star -->
    <div class="seek">
      <input
        v-model="seekQuery"
        class="seek-input"
        placeholder="seek a star…"
        @keydown.enter="seek"
      />
    </div>

    <!-- Folio: hovered/selected entity -->
    <aside v-if="focused" class="folio">
      <p class="folio-name">{{ focused.name }}</p>
      <p class="folio-meta">{{ focused.degree }} connections · constellation {{ focused.community }}</p>
      <RouterLink class="folio-link" :to="{ path: '/graph', query: { q: focused.name } }">
        open in the atlas →
      </RouterLink>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import { Graph } from '@cosmos.gl/graph'
import api from '@/api/client'

interface LiveGraph {
  names: string[]
  ids: string[]
  communities: number[]
  degrees: number[]
  links: number[]
  node_count: number
  link_count: number
}

const canvasHost = ref<HTMLDivElement | null>(null)
const ready = ref(false)
const nodeCount = ref(0)
const linkCount = ref(0)
const seekQuery = ref('')
const focused = ref<{ name: string; degree: number; community: number } | null>(null)

let cosmos: Graph | null = null
let data: LiveGraph | null = null

// Codex palette: gold-family constellations on deep parchment-night.
// Hue walks the warm band per community; large communities stay closest
// to the house gold so the whole sky reads as one manuscript.
function communityColor(c: number, alpha = 0.92): [number, number, number, number] {
  if (c < 0) return [154, 134, 110, alpha * 255] as unknown as [number, number, number, number]
  const hues = [
    [200, 169, 110], [214, 158, 94], [181, 146, 128], [226, 189, 122],
    [168, 148, 96], [204, 140, 92], [190, 170, 140], [172, 128, 100],
    [222, 174, 96], [158, 138, 118], [210, 186, 138], [186, 152, 84],
  ]
  const [r, g, b] = hues[c % hues.length]
  return [r, g, b, alpha * 255] as unknown as [number, number, number, number]
}

// Scale tiers: light laptops get a calm sky, big GPUs can ask for more.
// FPS watchdog downshifts automatically if the first seconds stutter.
const TIERS = [
  { label: 'faint', nodes: 1500, degree: 4 },
  { label: 'clear', nodes: 4000, degree: 3 },
  { label: 'deep', nodes: 12000, degree: 2 },
] as const
const tier = ref(1)

async function boot() {
  const t = TIERS[tier.value]
  const { data: g } = await api.get<LiveGraph>(
    `/api/graph/live?max_nodes=${t.nodes}&min_degree=${t.degree}`)
  data = g
  nodeCount.value = g.node_count
  linkCount.value = g.link_count
  if (!canvasHost.value) return

  const n = g.node_count
  const colors = new Float32Array(n * 4)
  const sizes = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    const [r, gg, b, a] = communityColor(g.communities[i])
    colors[i * 4] = r / 255
    colors[i * 4 + 1] = gg / 255
    colors[i * 4 + 2] = b / 255
    colors[i * 4 + 3] = a / 255
    // log-scaled by degree: hubs glow, leaves stay starlight
    sizes[i] = 1.5 + Math.log2(1 + g.degrees[i]) * 1.15
  }

  cosmos = new Graph(canvasHost.value, {
    backgroundColor: 'rgba(10, 8, 5, 1)',
    defaultLinkColor: 'rgba(200, 169, 110, 0.16)',
    defaultLinkWidth: 0.6,
    enableSimulation: true,
    simulationGravity: 0.12,
    simulationRepulsion: 1.1,
    simulationLinkSpring: 0.9,
    simulationFriction: 0.88,
    // Finite decay (2026-08-14, laptop-lag fix): the sim runs lively for
    // a few seconds and then RESTS — rendering stays interactive and any
    // drag/seek re-heats it. A never-settling sim is a space heater on
    // integrated GPUs; Obsidian's graph rests too.
    simulationDecay: 4000,
    // 4K laptops otherwise render 4x the pixels for no visible gain.
    pixelRatio: Math.min(window.devicePixelRatio || 1, 1.5),
    fitViewOnInit: true,
    fitViewDelay: 1200,
    renderHoveredPointRing: true,
    hoveredPointRingColor: 'rgba(226, 189, 122, 0.95)',
    focusedPointRingColor: 'rgba(226, 189, 122, 0.95)',
    onPointMouseOver: (index: number) => setFocus(index),
    onPointMouseOut: () => { focused.value = null },
    onClick: (index?: number) => { if (index !== undefined) setFocus(index) },
  })

  // cosmos derives the point count from the positions array — without it
  // there are no points at all. Seed a loose disc clustered by community
  // so related stars start near each other and the simulation converges
  // into constellations instead of untangling pure noise.
  const positions = new Float32Array(n * 2)
  const communityAngle = new Map<number, number>()
  for (let i = 0; i < n; i++) {
    const c = g.communities[i]
    if (!communityAngle.has(c)) {
      communityAngle.set(c, (communityAngle.size * 2.399963) % (Math.PI * 2))
    }
    const base = communityAngle.get(c)!
    const jitterA = base + (Math.random() - 0.5) * 0.9
    const jitterR = 1200 + Math.random() * 1800
    positions[i * 2] = Math.cos(jitterA) * jitterR + 4096
    positions[i * 2 + 1] = Math.sin(jitterA) * jitterR + 4096
  }
  startFpsWatchdog()
  cosmos.setPointPositions(positions)
  cosmos.setPointColors(colors)
  cosmos.setPointSizes(sizes)
  cosmos.setLinks(new Float32Array(g.links))
  cosmos.render()
  ready.value = true
}

let fpsRaf = 0
function startFpsWatchdog() {
  let frames = 0
  const started = performance.now()
  const tick = () => {
    frames++
    const elapsed = performance.now() - started
    if (elapsed < 4000) { fpsRaf = requestAnimationFrame(tick); return }
    const fps = (frames / elapsed) * 1000
    if (fps < 28 && tier.value > 0) {
      tier.value--
      reboot()
    }
  }
  fpsRaf = requestAnimationFrame(tick)
}

async function reboot() {
  cancelAnimationFrame(fpsRaf)
  cosmos?.destroy()
  cosmos = null
  ready.value = false
  focused.value = null
  await boot()
}

function setTier(i: number) {
  if (i === tier.value) return
  tier.value = i
  reboot()
}

function setFocus(index: number) {
  if (!data) return
  focused.value = {
    name: data.names[index],
    degree: data.degrees[index],
    community: data.communities[index],
  }
}

function stir() {
  // Wake the resting simulation with a gentle alpha — the constellations
  // shuffle and drift back to rest on the finite decay.
  cosmos?.start(0.35)
}

function refit() {
  cosmos?.fitView()
}

function seek() {
  if (!data || !cosmos) return
  const q = seekQuery.value.trim().toLowerCase()
  if (!q) return
  const i = data.names.findIndex((nm) => nm.toLowerCase().includes(q))
  if (i >= 0) {
    setFocus(i)
    cosmos.zoomToPointByIndex(i, 700, 6)
  }
}

onMounted(boot)
onUnmounted(() => {
  cancelAnimationFrame(fpsRaf)
  cosmos?.destroy()
  cosmos = null
})
</script>

<style scoped>
.constellarium {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  position: relative;
  height: calc(100vh - 64px);
  overflow: hidden;
  background: #0a0805;
}

.cosmos-host {
  position: absolute;
  inset: 0;
}

.plate {
  position: absolute;
  top: 28px;
  left: 32px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 1.4s ease 0.4s;
}
.ready .plate { opacity: 1; }

.plate-kicker {
  font-size: 11px;
  letter-spacing: 0.28em;
  color: var(--cp-gold-soft);
  margin: 0 0 6px;
}
.plate-title {
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 28px;
  font-weight: 400;
  color: var(--cp-ink);
  margin: 0 0 8px;
}
.plate-stats {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  color: var(--cp-ink-mute);
  margin: 0;
  pointer-events: auto;
}
.plate-stats em { color: var(--cp-gold); font-style: normal; }
.sep { margin: 0 8px; opacity: 0.5; }
.plate-act {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: var(--cp-gold);
  cursor: pointer;
  border-bottom: 1px dotted var(--cp-gold-soft);
}
.plate-act:hover { border-bottom-style: solid; }
.tier-picker { display: inline-flex; gap: 8px; }
.tier-opt { opacity: 0.55; border-bottom: none; }
.tier-opt.active { opacity: 1; border-bottom: 1px solid var(--cp-gold); }

.seek {
  position: absolute;
  top: 32px;
  right: 32px;
}
.seek-input {
  background: rgba(14, 11, 6, 0.72);
  border: 1px solid rgba(200, 169, 110, 0.28);
  border-radius: 2px;
  color: var(--cp-ink);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  padding: 7px 12px;
  width: 200px;
  outline: none;
  transition: border-color 0.3s;
}
.seek-input:focus { border-color: var(--cp-gold-soft); }
.seek-input::placeholder { color: rgba(238, 224, 196, 0.35); }

.folio {
  position: absolute;
  bottom: 30px;
  left: 32px;
  background: rgba(14, 11, 6, 0.82);
  border: 1px solid rgba(200, 169, 110, 0.25);
  border-left: 2px solid var(--cp-gold);
  border-radius: 2px;
  padding: 12px 18px;
  max-width: 340px;
  backdrop-filter: blur(6px);
}
.folio-name {
  font-family: Georgia, serif;
  font-size: 17px;
  color: var(--cp-ink);
  margin: 0 0 3px;
}
.folio-meta {
  font-size: 12px;
  font-style: italic;
  color: var(--cp-ink-mute);
  margin: 0 0 6px;
}
.folio-link {
  font-size: 12px;
  color: var(--cp-gold);
  text-decoration: none;
  border-bottom: 1px dotted var(--cp-gold-soft);
}
.folio-link:hover { border-bottom-style: solid; }

@media (max-width: 720px) {
  .plate { top: 16px; left: 16px; }
  .plate-title { font-size: 20px; }
  .seek { top: 14px; right: 16px; }
  .seek-input { width: 130px; }
  .folio { left: 16px; right: 16px; max-width: none; }
}
</style>
