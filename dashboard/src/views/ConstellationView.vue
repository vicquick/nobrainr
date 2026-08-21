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
        <span class="sep">·</span>
        <button class="plate-act" :class="{ active: showForces }" @click="showForces = !showForces">forces</button>
      </p>
      <p class="plate-stats" v-else>charting the heavens…</p>
    </header>

    <!-- Forces: Obsidian's four sliders, live on the running simulation -->
    <aside v-if="showForces" class="forces">
      <p class="forces-head">Forces</p>
      <label class="force-row">
        <span>Center force</span>
        <input type="range" min="0" max="1" step="0.05" v-model.number="fCenter" @input="applyForces" />
        <em>{{ fCenter.toFixed(2) }}</em>
      </label>
      <label class="force-row">
        <span>Repel force</span>
        <input type="range" min="0" max="2" step="0.05" v-model.number="fRepel" @input="applyForces" />
        <em>{{ fRepel.toFixed(2) }}</em>
      </label>
      <label class="force-row">
        <span>Link force</span>
        <input type="range" min="0" max="1" step="0.05" v-model.number="fLink" @input="applyForces" />
        <em>{{ fLink.toFixed(2) }}</em>
      </label>
      <label class="force-row">
        <span>Link distance</span>
        <input type="range" min="10" max="200" step="5" v-model.number="fDist" @input="applyForces" />
        <em>{{ fDist }}</em>
      </label>
      <label class="force-row">
        <span>Text fade</span>
        <input type="range" min="0.3" max="3" step="0.1" v-model.number="fOpacity" @input="updateLabelAlphas" />
        <em>{{ fOpacity.toFixed(1) }}</em>
      </label>
    </aside>

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
/**
 * Obsidian-faithful rebuild (2026-08-21). The previous Constellarium ran
 * cosmos.gl — a GPU particle simulator with its own shader integrator. It
 * showed 12k points, but no tuning could make it FEEL like Obsidian,
 * because the feel is not a parameter: it is d3-force's velocity-Verlet
 * settle plus a handful of very specific interaction behaviours. This view
 * now runs the exact stack of the canonical replica (Quartz v4): pixi.js
 * rendering, d3-force physics, mechanics verbatim from its source:
 *
 *   charge  = forceManyBody().strength(-100 * repelForce)
 *   center  = forceCenter().strength(centerForce)
 *   link    = forceLink().distance(linkDistance)  (+ strength slider)
 *   collide = forceCollide(nodeRadius)
 *   radius  = 2 + sqrt(degree)
 *   hover   → non-neighbours (nodes AND links) tween to α0.2 in 200ms
 *   label   → α = max((k · opacityScale − 1) / 3.75, 0), zoom-driven
 *   drag    → alphaTarget(1) reheat; fx offset divided by zoom k
 *
 * Honest tradeoff: d3-force is CPU. The old "deep" tier (12k) is now 5k —
 * Obsidian itself slows at that scale, and the feel matters more here than
 * raw count. The GPU full-sky view still exists at /galaxy.
 */
import { ref, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import { Application, Container, Graphics, Text, TextStyle } from 'pixi.js'
import {
  forceSimulation, forceManyBody, forceCenter, forceLink, forceCollide,
  type Simulation, type SimulationNodeDatum,
} from 'd3-force'
import { select } from 'd3-selection'
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from 'd3-zoom'
import { drag } from 'd3-drag'
import { Group as TweenGroup, Tween } from '@tweenjs/tween.js'
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
interface StarNode extends SimulationNodeDatum {
  idx: number
  id: string
  name: string
  community: number
  degree: number
  gfx?: Graphics
  label?: Text
  active: boolean
  fx?: number | null
  fy?: number | null
}
interface StarLink {
  source: StarNode
  target: StarNode
  active: boolean
}

const canvasHost = ref<HTMLDivElement | null>(null)
const ready = ref(false)
const nodeCount = ref(0)
const linkCount = ref(0)
const seekQuery = ref('')
const focused = ref<{ name: string; degree: number; community: number } | null>(null)
const showForces = ref(false)

/* Obsidian slider defaults — Quartz ships 0.5 / 0.3 / 30. Link force 1.0
   keeps d3's default per-link strength curve as the baseline. */
const fCenter = ref(0.3)
const fRepel = ref(0.5)
const fLink = ref(1.0)
const fDist = ref(30)
const fOpacity = ref(1.3)

/* CPU physics budget, not GPU: sized so charge + collide hold frame rate.
   The 12k sky lives on /galaxy. */
const TIERS = [
  { label: 'faint', nodes: 800, degree: 5 },
  { label: 'clear', nodes: 2000, degree: 3 },
  { label: 'deep', nodes: 5000, degree: 2 },
] as const
const tier = ref(1)

// Codex palette: gold-family constellations on deep parchment-night.
// Kept verbatim from the cosmos version — the house voice, not a formula.
const HUES: Array<[number, number, number]> = [
  [200, 169, 110], [214, 158, 94], [181, 146, 128], [226, 189, 122],
  [168, 148, 96], [204, 140, 92], [190, 170, 140], [172, 128, 100],
  [222, 174, 96], [158, 138, 118], [210, 186, 138], [186, 152, 84],
]
function communityColor(c: number): number {
  const [r, g, b] = c < 0 ? [154, 134, 110] : HUES[c % HUES.length]
  return (r << 16) | (g << 8) | b
}
const LINK_DIM = 0x3a3226
const LINK_LIT = 0x8a7a58

let app: Application | null = null
let stage: Container | null = null
let linkGfx: Graphics | null = null
let nodeLayer: Container | null = null
let labelLayer: Container | null = null
let simulation: Simulation<StarNode, undefined> | null = null
let zoomB: ZoomBehavior<HTMLCanvasElement, unknown> | null = null
let nodes: StarNode[] = []
let links: StarLink[] = []
let currentTransform: ZoomTransform = zoomIdentity
let hoveredId: number | null = null
let dragging = false
let destroyed = false

/* One tween group per concern so a fresh hover cancels only its own kind —
   how Quartz stops rapid hover-jumps fighting themselves. */
const tweens = new Map<string, TweenGroup>()
function retween(key: string): TweenGroup {
  tweens.get(key)?.getAll().forEach((t) => t.stop())
  const group = new TweenGroup()
  tweens.set(key, group)
  return group
}

/* radius = 2 + sqrt(degree), capped: our hub entities reach degree 500+,
   which no Obsidian vault sees — uncapped they would render as plates. */
function nodeRadius(d: StarNode): number {
  return Math.min(2 + Math.sqrt(d.degree), 14)
}

/* All-node labels would mean 5k pixi Texts; Obsidian vaults are hundreds.
   Label the named tier of the sky and materialise on hover for the rest —
   visually indistinguishable, since faint stars' labels are zoom-faded to
   zero anyway. */
const LABEL_BUDGET = 400
const labelStyle = new TextStyle({
  fontFamily: "Georgia, 'Palatino Linotype', serif",
  fontSize: 11,
  fill: 0xeee0c4,
})

/* The exact Obsidian/Quartz label law. */
function labelAlphaForZoom(): number {
  const scale = currentTransform.k * fOpacity.value
  return Math.max((scale - 1) / 3.75, 0)
}

function ensureLabel(n: StarNode): Text {
  if (n.label) return n.label
  const t = new Text({ text: n.name, style: labelStyle })
  t.anchor.set(0.5, 0)
  t.alpha = 0
  n.label = t
  labelLayer!.addChild(t)
  return t
}

function neighboursOf(id: number): Set<number> {
  const set = new Set<number>([id])
  for (const l of links) {
    if (l.source.idx === id) set.add(l.target.idx)
    if (l.target.idx === id) set.add(l.source.idx)
  }
  return set
}

function updateHover(newId: number | null) {
  hoveredId = newId
  const hood = newId === null ? new Set<number>() : neighboursOf(newId)
  for (const n of nodes) n.active = newId !== null && hood.has(n.idx)
  for (const l of links) l.active = newId !== null && (l.source.idx === newId || l.target.idx === newId)

  /* The signature: everything outside the neighbourhood fades to 0.2 over
     200ms; the hovered label pops to full alpha at 1.1× over 100ms. */
  const hoverG = retween('hover')
  for (const n of nodes) {
    if (!n.gfx) continue
    const alpha = newId === null ? 1 : n.active ? 1 : 0.2
    hoverG.add(new Tween(n.gfx).to({ alpha }, 200).start())
  }
  const labelG = retween('label')
  const restingAlpha = labelAlphaForZoom()
  for (const n of nodes) {
    if (newId === n.idx) {
      const t = ensureLabel(n)
      labelG.add(new Tween(t).to({ alpha: 1 }, 100).start())
      labelG.add(new Tween(t.scale).to({ x: 1.1, y: 1.1 }, 100).start())
    } else if (n.label) {
      labelG.add(new Tween(n.label).to({ alpha: n.active ? Math.max(restingAlpha, 0.85) : restingAlpha }, 100).start())
      labelG.add(new Tween(n.label.scale).to({ x: 1, y: 1 }, 100).start())
    }
  }
}

function updateLabelAlphas() {
  const a = labelAlphaForZoom()
  for (const n of nodes) {
    if (n.label && !n.active && hoveredId !== n.idx) n.label.alpha = a
  }
}

function drawLinks() {
  if (!linkGfx) return
  linkGfx.clear()
  const fading = hoveredId !== null
  for (const l of links) {
    linkGfx.moveTo(l.source.x!, l.source.y!)
    linkGfx.lineTo(l.target.x!, l.target.y!)
    linkGfx.stroke({
      width: 1,
      color: l.active ? LINK_LIT : LINK_DIM,
      alpha: fading ? (l.active ? 1 : 0.15) : 0.55,
    })
  }
}

function tickRender() {
  for (const n of nodes) {
    if (n.gfx) n.gfx.position.set(n.x!, n.y!)
    if (n.label) n.label.position.set(n.x!, n.y! + nodeRadius(n) + 3)
  }
  drawLinks()
}

function applyForces() {
  if (!simulation) return
  simulation
    .force('charge', forceManyBody().strength(-100 * fRepel.value))
    .force('center', forceCenter(0, 0).strength(fCenter.value))
    .force('link', forceLink<StarNode, StarLink>(links)
      .distance(fDist.value)
      .strength((l) => fLink.value / Math.min(l.source.degree, l.target.degree, 10)))
  simulation.alpha(0.4).restart()
}

async function build(payload: LiveGraph) {
  const host = canvasHost.value!
  app = new Application()
  await app.init({
    background: 0x0a0805,
    resizeTo: host,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 1.5),
  })
  if (destroyed) { app.destroy(true); return }
  host.appendChild(app.canvas)

  stage = new Container()
  app.stage.addChild(stage)
  linkGfx = new Graphics()
  nodeLayer = new Container()
  labelLayer = new Container()
  stage.addChild(linkGfx, nodeLayer, labelLayer)

  /* Seed each community around its own bearing so the sim untangles into
     constellations rather than pure noise (kept from the cosmos version). */
  const communityAngle = new Map<number, number>()
  nodes = payload.names.map((name, i) => {
    const c = payload.communities[i]
    if (!communityAngle.has(c)) {
      communityAngle.set(c, (communityAngle.size * 2.399963) % (Math.PI * 2))
    }
    const base = communityAngle.get(c)!
    const a = base + (Math.random() - 0.5) * 0.9
    const r = 160 + Math.random() * 240
    return {
      idx: i,
      id: payload.ids[i],
      name,
      community: c,
      degree: payload.degrees[i],
      active: false,
      x: Math.cos(a) * r,
      y: Math.sin(a) * r,
    }
  })
  links = []
  for (let i = 0; i < payload.links.length; i += 2) {
    links.push({
      source: nodes[payload.links[i]],
      target: nodes[payload.links[i + 1]],
      active: false,
    })
  }
  nodeCount.value = nodes.length
  linkCount.value = links.length

  for (const n of nodes) {
    const g = new Graphics()
    g.circle(0, 0, nodeRadius(n)).fill(communityColor(n.community))
    g.eventMode = 'static'
    g.cursor = 'pointer'
    g.on('pointerover', () => {
      if (dragging) return
      updateHover(n.idx)
      focused.value = n
    })
    g.on('pointerleave', () => { if (!dragging) updateHover(null) })
    nodeLayer.addChild(g)
    n.gfx = g
  }
  const byDegree = [...nodes].sort((a, b) => b.degree - a.degree).slice(0, LABEL_BUDGET)
  for (const n of byDegree) ensureLabel(n)

  /* physics — the exact Obsidian/Quartz recipe */
  simulation = forceSimulation<StarNode>(nodes)
    .force('collide', forceCollide<StarNode>((n) => nodeRadius(n))
      .iterations(nodes.length > 2500 ? 1 : 3))
    .on('tick', tickRender)
  applyForces()

  /* zoom + pan. Centering lives inside the transform (Quartz pattern):
     stage.position = transform.x/y, stage.scale = k — nothing else. */
  const canvas = app.canvas as HTMLCanvasElement
  zoomB = zoom<HTMLCanvasElement, unknown>()
    .scaleExtent([0.15, 6])
    /* wheel always zooms; drags only pan when not over a star, so the
       node drag behaviour below wins that gesture */
    .filter((e: any) => !e.button && (e.type === 'wheel' || hoveredId === null))
    .on('zoom', (e) => {
      currentTransform = e.transform
      stage!.scale.set(e.transform.k)
      stage!.position.set(e.transform.x, e.transform.y)
      updateLabelAlphas()
    })
  select(canvas).call(zoomB)
  select(canvas).call(
    zoomB.transform,
    zoomIdentity.translate(host.clientWidth / 2, host.clientHeight / 2),
  )

  /* node drag — verbatim mechanics: reheat on grab, fx delta divided by
     zoom k so the node tracks the cursor at any magnification, release
     un-pins so the node re-floats. Short grab = click = pin the folio. */
  let dragStart = 0
  const dragB = drag<HTMLCanvasElement, unknown>()
    .container(() => canvas)
    .subject(() => (hoveredId !== null ? nodes[hoveredId] : undefined) as any)
    .on('start', (e: any) => {
      if (!e.active) simulation!.alphaTarget(1).restart()
      e.subject.fx = e.subject.x
      e.subject.fy = e.subject.y
      e.subject.__initialDragPos = { x: e.subject.x, y: e.subject.y }
      dragStart = Date.now()
      dragging = true
    })
    .on('drag', (e: any) => {
      const p = e.subject.__initialDragPos
      e.subject.fx = p.x + (e.x - p.x) / currentTransform.k
      e.subject.fy = p.y + (e.y - p.y) / currentTransform.k
    })
    .on('end', (e: any) => {
      if (!e.active) simulation!.alphaTarget(0)
      e.subject.fx = null
      e.subject.fy = null
      dragging = false
      if (Date.now() - dragStart < 400) focused.value = e.subject
    })
  select(canvas).call(dragB as any)

  /* tween pump */
  app.ticker.add(() => {
    const now = performance.now()
    for (const group of tweens.values()) group.update(now)
  })

  ready.value = true
}

async function load() {
  const t = TIERS[tier.value]
  const { data } = await api.get<LiveGraph>(
    `/api/graph/live?max_nodes=${t.nodes}&min_degree=${t.degree}`,
  )
  if (!destroyed && canvasHost.value) await build(data)
}

function teardown() {
  simulation?.stop()
  simulation = null
  tweens.forEach((g) => g.getAll().forEach((t) => t.stop()))
  tweens.clear()
  hoveredId = null
  if (app) { app.destroy(true, { children: true }); app = null }
  stage = null; linkGfx = null; nodeLayer = null; labelLayer = null
  nodes = []; links = []
  currentTransform = zoomIdentity
  ready.value = false
  focused.value = null
}

function setTier(i: number) {
  if (i === tier.value) return
  tier.value = i
  teardown()
  load()
}

function stir() { simulation?.alpha(0.6).restart() }

function refit() {
  if (!app || !zoomB || !canvasHost.value) return
  select(app.canvas as HTMLCanvasElement).call(
    zoomB.transform,
    zoomIdentity.translate(canvasHost.value.clientWidth / 2, canvasHost.value.clientHeight / 2),
  )
}

function seek() {
  const q = seekQuery.value.trim().toLowerCase()
  if (!q || !app || !zoomB || !canvasHost.value) return
  const hit = nodes.find((n) => n.name.toLowerCase().includes(q))
  if (!hit) return
  focused.value = hit
  updateHover(hit.idx)
  const host = canvasHost.value
  const k = Math.max(currentTransform.k, 1.8)
  select(app.canvas as HTMLCanvasElement).call(
    zoomB.transform,
    zoomIdentity
      .translate(host.clientWidth / 2 - hit.x! * k, host.clientHeight / 2 - hit.y! * k)
      .scale(k),
  )
}

onMounted(load)
onUnmounted(() => { destroyed = true; teardown() })
</script>

<style scoped>
.constellarium {
  position: relative;
  height: calc(100vh - 64px);
  overflow: hidden;
  background: #0a0805;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: rgba(238, 224, 196, 0.94);
}
.cosmos-host { position: absolute; inset: 0; }
.cosmos-host :deep(canvas) { display: block; }

.plate {
  position: absolute; top: 22px; left: 28px; z-index: 2; pointer-events: none;
}
.plate-kicker {
  font-size: 9px; letter-spacing: 0.28em; color: #c8a96e;
  margin: 0 0 4px; font-style: italic;
}
.plate-title {
  font-size: clamp(22px, 3vw, 30px); font-weight: 400; margin: 0 0 6px;
}
.plate-stats { font-size: 11.5px; color: rgba(238, 224, 196, 0.55); margin: 0; pointer-events: auto; }
.plate-stats em { color: #c8a96e; font-style: normal; }
.sep { margin: 0 7px; color: rgba(200, 169, 110, 0.4); }
.plate-act {
  background: none; border: none; color: #c8a96e; cursor: pointer;
  font: inherit; font-style: italic; padding: 0;
  border-bottom: 1px dotted rgba(200, 169, 110, 0.4);
}
.plate-act:hover { color: #e2bd7a; }
.plate-act.active { border-bottom-style: solid; }
.tier-opt { margin-right: 8px; opacity: 0.55; }
.tier-opt.active { opacity: 1; border-bottom-style: solid; }

.forces {
  position: absolute; top: 118px; left: 28px; z-index: 3;
  background: rgba(14, 11, 6, 0.92);
  border: 1px solid rgba(200, 169, 110, 0.18);
  padding: 14px 16px; width: 248px;
  backdrop-filter: blur(6px);
}
.forces-head {
  font-size: 10px; letter-spacing: 0.2em; text-transform: uppercase;
  color: #c8a96e; margin: 0 0 10px; font-style: italic;
}
.force-row {
  display: grid; grid-template-columns: 84px 1fr 34px; align-items: center;
  gap: 8px; font-size: 11px; margin-bottom: 8px;
  color: rgba(238, 224, 196, 0.7);
}
.force-row em { font-style: normal; color: #c8a96e; text-align: right; }
.force-row input[type='range'] { accent-color: #c8a96e; }

.seek { position: absolute; top: 26px; right: 28px; z-index: 2; }
.seek-input {
  background: rgba(14, 11, 6, 0.85);
  border: 1px solid rgba(200, 169, 110, 0.25);
  color: rgba(238, 224, 196, 0.94);
  padding: 7px 12px; width: 190px;
  font: inherit; font-size: 12px; font-style: italic; outline: none;
}
.seek-input:focus { border-color: rgba(200, 169, 110, 0.6); }
.seek-input::placeholder { color: rgba(238, 224, 196, 0.35); }

.folio {
  position: absolute; bottom: 26px; right: 28px; z-index: 2;
  background: rgba(14, 11, 6, 0.92);
  border: 1px solid rgba(200, 169, 110, 0.22);
  padding: 14px 18px; max-width: 300px;
  backdrop-filter: blur(6px);
}
.folio-name { font-size: 15px; margin: 0 0 3px; color: #e2bd7a; }
.folio-meta { font-size: 11px; color: rgba(238, 224, 196, 0.55); margin: 0 0 8px; font-style: italic; }
.folio-link {
  font-size: 11px; color: #c8a96e; text-decoration: none; font-style: italic;
  border-bottom: 1px dotted rgba(200, 169, 110, 0.4);
}
.folio-link:hover { color: #e2bd7a; }

@media (max-width: 640px) {
  .plate { left: 16px; top: 14px; }
  .seek { top: 14px; right: 16px; }
  .seek-input { width: 130px; }
  .forces { left: 16px; width: 216px; }
  .folio { left: 16px; right: 16px; max-width: none; }
}
</style>
