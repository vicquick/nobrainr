<template>
  <div class="constellarium" :class="{ ready }">
    <div ref="canvasHost" class="cosmos-host" />

    <p v-if="webglMissing" class="webgl-note">
      The constellarium is drawn with WebGL, which this browser has disabled.
    </p>

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

    <!-- Folio: pinned (click) beats hovered; pinned is dismissable -->
    <aside v-if="folio" class="folio">
      <button v-if="pinnedFocus" class="folio-close" @click="pinnedFocus = null" title="unpin">×</button>
      <p class="folio-name">{{ folio.name }}</p>
      <p class="folio-meta">{{ folio.degree }} connections · constellation {{ folio.community }}</p>
      <RouterLink class="folio-link" :to="{ path: '/graph', query: { q: folio.name } }">
        open in the atlas →
      </RouterLink>
    </aside>
  </div>
</template>

<script setup lang="ts">
/**
 * Obsidian-faithful Constellarium, round 3 (2026-08-22).
 *
 * Round 2 fixed the interaction structure but missed the elephant an
 * adversarial verification pass then measured: this graph's DEFAULT tier
 * carries ~58,000 links (faint ~31k, deep ~106k) — ~30x an Obsidian
 * vault — and pixi v8's Graphics.stroke() clones a path and re-tessellates
 * EVERY instruction whenever the context is dirty (GraphicsContext.js:174,
 * verified against pixi 8.20). One shared Graphics redrawn per frame meant
 * 58k tessellations at 60fps: a slideshow. Round 2's "render every frame"
 * fix made it unconditional.
 *
 * Round 3's render core:
 *  - LINKS ARE ONE GPU MESH. 4 verts / 6 indices per link; a position
 *    buffer rewritten in-place only while geometry can actually change
 *    (sim alpha above rest, or a node drag), and a per-vertex style
 *    attribute (alpha, litness) written only while a hover transition is
 *    in flight. No tessellation, ever.
 *  - HOVER IS ONE SHARED ANIMATION, not per-object tweens. Round 2
 *    allocated a Tween per node AND per link on every pointerover — 60k
 *    allocations per hover. Now: from/to arrays and a single 200ms
 *    progress value advanced on the ticker. Labels (≤ ~400) keep real
 *    tweens; that is the scale tweens are for.
 *  - eventMode 'static' + explicit Circle hitArea (cheap math test instead
 *    of containsPoint over every Graphics context). 'dynamic' re-synthesises
 *    pointer events between real moves and flapped hover at hit-circle rims.
 *  - click-to-pin decided in d3-drag's end handler by pointer distance;
 *    grab radius mirrors the hitArea, so an empty-sky press pans instead of
 *    silently dragging an unseen star.
 *  - generation guard around the async build: a tier switch mid-init used
 *    to destroy a not-yet-initialised Application.
 *  - Vue refs receive plain snapshots, never StarNode (which drags pixi
 *    Graphics/Text into Vue's reactive proxy).
 *
 * The Obsidian mechanics themselves are unchanged from round 2 and match
 * the Quartz reference: charge -100*repel / center / linkDistance /
 * collide; radius 2+sqrt(subgraph degree); hover dims non-neighbours to
 * 0.2 in 200ms; labels obey a = max((k*opacityScale-1)/3.75, 0); drag
 * reheats alphaTarget(1) with the fx delta divided by zoom k; drag is
 * attached BEFORE zoom so d3's own nopropagation arbitrates the gesture.
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Application, Container, Graphics, Text, TextStyle, Circle,
  Mesh, MeshGeometry, Shader, GlProgram, Buffer, BufferUsage,
} from 'pixi.js'
import {
  forceSimulation, forceManyBody, forceCenter, forceLink, forceCollide,
  type Simulation, type SimulationNodeDatum,
} from 'd3-force'
import { select } from 'd3-selection'
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from 'd3-zoom'
import { drag } from 'd3-drag'
import 'd3-transition'
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
  subDegree: number
  gfx?: Graphics
  label?: Text
  fx?: number | null
  fy?: number | null
}
interface FocusInfo { name: string; degree: number; community: number; idx: number }

const canvasHost = ref<HTMLDivElement | null>(null)
const ready = ref(false)
const webglMissing = ref(false)
const nodeCount = ref(0)
const linkCount = ref(0)
const seekQuery = ref('')
const hoverFocus = ref<FocusInfo | null>(null)
const pinnedFocus = ref<FocusInfo | null>(null)
const folio = computed(() => pinnedFocus.value ?? hoverFocus.value)
const showForces = ref(false)

const fCenter = ref(0.3)
const fRepel = ref(0.5)
const fLink = ref(1.0)
const fDist = ref(30)
const fOpacity = ref(1.3)

const TIERS = [
  { label: 'faint', nodes: 800, degree: 5 },
  { label: 'clear', nodes: 2000, degree: 3 },
  { label: 'deep', nodes: 5000, degree: 2 },
] as const
const tier = ref(1)

const HUES: Array<[number, number, number]> = [
  [200, 169, 110], [214, 158, 94], [181, 146, 128], [226, 189, 122],
  [168, 148, 96], [204, 140, 92], [190, 170, 140], [172, 128, 100],
  [222, 174, 96], [158, 138, 118], [210, 186, 138], [186, 152, 84],
]
function communityColor(c: number): number {
  const [r, g, b] = c < 0 ? [154, 134, 110] : HUES[c % HUES.length]
  return (r << 16) | (g << 8) | b
}

let app: Application | null = null
let stage: Container | null = null
let nodeLayer: Container | null = null
let labelLayer: Container | null = null
let simulation: Simulation<StarNode, undefined> | null = null
let zoomB: ZoomBehavior<HTMLCanvasElement, unknown> | null = null
let nodes: StarNode[] = []
/* links as flat index pairs; per-link state lives in typed arrays that feed
   the mesh directly — no per-link objects at 58k scale */
let linkPairs: Uint32Array = new Uint32Array(0)
let adjacency: Int32Array[] = []
let currentTransform: ZoomTransform = zoomIdentity
let hoveredId: number | null = null
let highlightId: number | null = null
let dragging = false
let destroyed = false
/* generation guard: a tier switch mid-await must not destroy the app the
   NEXT build is initialising */
let buildGen = 0

/* ── link mesh state ─────────────────────────────────────────────── */
let linkMesh: Mesh | null = null
let posBuf: Buffer | null = null
let styleBuf: Buffer | null = null
let posArr: Float32Array = new Float32Array(0)
let styleArr: Float32Array = new Float32Array(0) // (alpha, lit) per vertex
const LINK_HALF_W = 0.5
const LINK_BASE_ALPHA = 0.55

/* ── shared hover animation (one progress value, zero allocations) ── */
let nodeAlphaCur: Float32Array = new Float32Array(0)
let nodeAlphaFrom: Float32Array = new Float32Array(0)
let nodeAlphaTo: Float32Array = new Float32Array(0)
let linkAlphaCur: Float32Array = new Float32Array(0)
let linkAlphaFrom: Float32Array = new Float32Array(0)
let linkAlphaTo: Float32Array = new Float32Array(0)
let linkLitCur: Float32Array = new Float32Array(0)
let linkLitFrom: Float32Array = new Float32Array(0)
let linkLitTo: Float32Array = new Float32Array(0)
let hoverAnimStart = 0
let hoverAnimActive = false
const HOVER_MS = 200

const tweens = new Map<string, TweenGroup>()
function retween(key: string): TweenGroup {
  tweens.get(key)?.getAll().forEach((t) => t.stop())
  const group = new TweenGroup()
  tweens.set(key, group)
  return group
}

function nodeRadius(d: StarNode): number {
  return Math.min(2 + Math.sqrt(d.subDegree), 20)
}

const LABEL_BUDGET = 400
const labelStyle = new TextStyle({
  fontFamily: "Georgia, 'Palatino Linotype', serif",
  fontSize: 11,
  fill: 0xeee0c4,
})

function labelAlphaForZoom(): number {
  const scale = currentTransform.k * fOpacity.value
  return Math.max((scale - 1) / 3.75, 0)
}

function ensureLabel(n: StarNode): Text {
  if (n.label) return n.label
  const t = new Text({ text: n.name, style: labelStyle })
  t.anchor.set(0.5, 0)
  t.alpha = 0
  t.position.set(n.x ?? 0, (n.y ?? 0) + nodeRadius(n) + 3)
  n.label = t
  labelLayer!.addChild(t)
  return t
}

function snapshot(n: StarNode): FocusInfo {
  /* plain object — never hand a StarNode (with pixi children) to a Vue ref */
  return { name: n.name, degree: n.degree, community: n.community, idx: n.idx }
}

/* The signature interaction. Computes TARGET alphas into flat arrays and
   arms the single shared 200ms animation; the ticker does the lerping. */
function applyHighlight(newId: number | null) {
  highlightId = newId
  const n = nodes.length
  const L = linkPairs.length / 2
  if (newId === null) {
    nodeAlphaTo.fill(1)
    linkAlphaTo.fill(LINK_BASE_ALPHA)
    linkLitTo.fill(0)
  } else {
    nodeAlphaTo.fill(0.2)
    linkAlphaTo.fill(0.2 * LINK_BASE_ALPHA)
    linkLitTo.fill(0)
    nodeAlphaTo[newId] = 1
    const adj = adjacency[newId]
    for (let i = 0; i < adj.length; i++) nodeAlphaTo[adj[i]] = 1
    for (let li = 0; li < L; li++) {
      const a = linkPairs[li * 2], b = linkPairs[li * 2 + 1]
      if (a === newId || b === newId) {
        linkAlphaTo[li] = 1
        linkLitTo[li] = 1
      }
    }
  }
  nodeAlphaFrom.set(nodeAlphaCur)
  linkAlphaFrom.set(linkAlphaCur)
  linkLitFrom.set(linkLitCur)
  hoverAnimStart = performance.now()
  hoverAnimActive = true

  /* labels: few enough for real tweens */
  const labelG = retween('label')
  const restingAlpha = labelAlphaForZoom()
  for (const nd of nodes) {
    if (newId === nd.idx) {
      const t = ensureLabel(nd)
      labelG.add(new Tween(t).to({ alpha: 1 }, 100).start())
      labelG.add(new Tween(t.scale).to({ x: 1.1, y: 1.1 }, 100).start())
    } else if (nd.label) {
      labelG.add(new Tween(nd.label).to({ alpha: restingAlpha }, 100).start())
      labelG.add(new Tween(nd.label.scale).to({ x: 1, y: 1 }, 100).start())
    }
  }
}

function updateLabelAlphas() {
  const a = labelAlphaForZoom()
  for (const n of nodes) {
    if (n.label && highlightId !== n.idx) n.label.alpha = a
  }
}

/* geometry pass: rewrite the mesh position buffer from sim coordinates.
   Runs only while positions can actually change. */
function writeLinkGeometry() {
  const L = linkPairs.length / 2
  for (let li = 0; li < L; li++) {
    const s = nodes[linkPairs[li * 2]], t = nodes[linkPairs[li * 2 + 1]]
    const x1 = s.x!, y1 = s.y!, x2 = t.x!, y2 = t.y!
    let dx = x2 - x1, dy = y2 - y1
    const len = Math.hypot(dx, dy) || 1
    const nx = (-dy / len) * LINK_HALF_W, ny = (dx / len) * LINK_HALF_W
    const o = li * 8
    posArr[o] = x1 + nx;     posArr[o + 1] = y1 + ny
    posArr[o + 2] = x1 - nx; posArr[o + 3] = y1 - ny
    posArr[o + 4] = x2 + nx; posArr[o + 5] = y2 + ny
    posArr[o + 6] = x2 - nx; posArr[o + 7] = y2 - ny
  }
  posBuf!.update()
}

function writeLinkStyle() {
  const L = linkPairs.length / 2
  for (let li = 0; li < L; li++) {
    const a = linkAlphaCur[li], lit = linkLitCur[li]
    const o = li * 8
    styleArr[o] = a;     styleArr[o + 1] = lit
    styleArr[o + 2] = a; styleArr[o + 3] = lit
    styleArr[o + 4] = a; styleArr[o + 5] = lit
    styleArr[o + 6] = a; styleArr[o + 7] = lit
  }
  styleBuf!.update()
}

function syncNodeVisuals() {
  for (const n of nodes) {
    if (n.gfx) n.gfx.position.set(n.x!, n.y!)
    if (n.label) n.label.position.set(n.x!, n.y! + nodeRadius(n) + 3)
  }
}

function applyForces(alpha = 0.4) {
  if (!simulation) return
  const d3links = [] as Array<{ source: StarNode; target: StarNode }>
  for (let i = 0; i < linkPairs.length; i += 2) {
    d3links.push({ source: nodes[linkPairs[i]], target: nodes[linkPairs[i + 1]] })
  }
  simulation
    .force('charge', forceManyBody().strength(-100 * fRepel.value))
    .force('center', forceCenter(0, 0).strength(fCenter.value))
    .force('link', forceLink<StarNode, { source: StarNode; target: StarNode }>(d3links)
      .distance(fDist.value)
      .strength((l) => fLink.value / Math.min(l.source.subDegree || 1, l.target.subDegree || 1)))
  simulation.alpha(alpha).restart()
}

async function build(payload: LiveGraph) {
  const gen = ++buildGen
  const host = canvasHost.value!
  const newApp = new Application()
  await newApp.init({
    background: 0x0a0805,
    resizeTo: host,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 1.5),
    preference: 'webgl', // the link shader is GLSL; keep the renderer deterministic
  })
  if (destroyed || gen !== buildGen) { newApp.destroy(true); return }
  app = newApp
  /* the link mesh below is a raw GL shader; pixi's canvas fallback has no
     pipe for it and throws on EVERY render — measured as a 60fps uncaught-
     exception storm that froze the world (stale worldTransforms, dead hit
     tests) and leaked MBs/s into any attached CDP client. No WebGL → say
     so once and stop, instead of storming. */
  if (app.renderer.name !== 'webgl' && app.renderer.name !== 'webgpu') {
    app.destroy(true, { children: true })
    app = null
    webglMissing.value = true
    return
  }
  /* pixi v8 preventDefaults pointerdown by default, and a cancelled
     pointerdown suppresses the browser's COMPATIBILITY mousedown (spec) —
     which is the only event d3-drag and d3-zoom listen to. With the
     default, every mouse gesture except wheel is silently dead. */
  ;(app.renderer.events as any).autoPreventDefault = false
  ;(app.canvas as HTMLCanvasElement).style.touchAction = 'none'
  host.appendChild(app.canvas)

  stage = new Container()
  app.stage.addChild(stage)
  if (import.meta.env.DEV) (window as any).__app = app /* behaviour-suite seam */
  nodeLayer = new Container()
  labelLayer = new Container()

  /* ── data ── */
  const subDeg = new Map<number, number>()
  for (let i = 0; i < payload.links.length; i += 2) {
    subDeg.set(payload.links[i], (subDeg.get(payload.links[i]) || 0) + 1)
    subDeg.set(payload.links[i + 1], (subDeg.get(payload.links[i + 1]) || 0) + 1)
  }
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
      idx: i, id: payload.ids[i], name, community: c,
      degree: payload.degrees[i], subDegree: subDeg.get(i) || 0,
      x: Math.cos(a) * r, y: Math.sin(a) * r,
    }
  })
  linkPairs = new Uint32Array(payload.links)
  const N = nodes.length, L = linkPairs.length / 2
  nodeCount.value = N
  linkCount.value = L

  /* adjacency once — applyHighlight must not rescan 58k links per hover */
  const adjCount = new Uint32Array(N)
  for (let i = 0; i < linkPairs.length; i += 2) { adjCount[linkPairs[i]]++; adjCount[linkPairs[i + 1]]++ }
  adjacency = Array.from({ length: N }, (_, i) => new Int32Array(adjCount[i]))
  const cursor = new Uint32Array(N)
  for (let i = 0; i < linkPairs.length; i += 2) {
    const a = linkPairs[i], b = linkPairs[i + 1]
    adjacency[a][cursor[a]++] = b
    adjacency[b][cursor[b]++] = a
  }

  /* ── link mesh: 4 verts + 6 indices per link, one draw call ── */
  posArr = new Float32Array(L * 8)
  styleArr = new Float32Array(L * 8)
  const indices = new Uint32Array(L * 6)
  for (let li = 0; li < L; li++) {
    const v = li * 4, o = li * 6
    indices[o] = v; indices[o + 1] = v + 1; indices[o + 2] = v + 2
    indices[o + 3] = v + 1; indices[o + 4] = v + 3; indices[o + 5] = v + 2
  }
  posBuf = new Buffer({ data: posArr, usage: BufferUsage.VERTEX | BufferUsage.COPY_DST })
  styleBuf = new Buffer({ data: styleArr, usage: BufferUsage.VERTEX | BufferUsage.COPY_DST })
  const geometry = new MeshGeometry({ positions: new Float32Array(0), indices })
  geometry.addAttribute('aPosition', { buffer: posBuf, format: 'float32x2' })
  geometry.addAttribute('aStyle', { buffer: styleBuf, format: 'float32x2' })
  const glProgram = GlProgram.from({
    vertex: `
      in vec2 aPosition;
      in vec2 aStyle;
      out vec2 vStyle;
      uniform mat3 uProjectionMatrix;
      uniform mat3 uWorldTransformMatrix;
      uniform mat3 uTransformMatrix;
      void main() {
        mat3 mvp = uProjectionMatrix * uWorldTransformMatrix * uTransformMatrix;
        gl_Position = vec4((mvp * vec3(aPosition, 1.0)).xy, 0.0, 1.0);
        vStyle = aStyle;
      }`,
    fragment: `
      in vec2 vStyle;
      out vec4 finalColor;
      void main() {
        vec3 dim = vec3(0.227, 0.196, 0.149);   /* 0x3a3226 */
        vec3 lit = vec3(0.541, 0.478, 0.345);   /* 0x8a7a58 */
        vec3 c = mix(dim, lit, clamp(vStyle.y, 0.0, 1.0));
        float a = vStyle.x;
        finalColor = vec4(c * a, a);            /* premultiplied */
      }`,
  })
  linkMesh = new Mesh({ geometry, shader: new Shader({ glProgram }) })
  stage.addChild(linkMesh, nodeLayer, labelLayer)

  /* ── hover animation arrays ── */
  nodeAlphaCur = new Float32Array(N).fill(1)
  nodeAlphaFrom = new Float32Array(N).fill(1)
  nodeAlphaTo = new Float32Array(N).fill(1)
  linkAlphaCur = new Float32Array(L).fill(LINK_BASE_ALPHA)
  linkAlphaFrom = new Float32Array(L).fill(LINK_BASE_ALPHA)
  linkAlphaTo = new Float32Array(L).fill(LINK_BASE_ALPHA)
  linkLitCur = new Float32Array(L)
  linkLitFrom = new Float32Array(L)
  linkLitTo = new Float32Array(L)
  writeLinkStyle()

  /* ── node sprites ── */
  for (const n of nodes) {
    const g = new Graphics()
    g.circle(0, 0, nodeRadius(n)).fill(communityColor(n.community))
    /* 'static', matching the reference. 'dynamic' re-synthesizes hit tests
       every ticker frame, which FLAPS hover at the hit-circle rim (measured:
       folio dying on mousedown because hoveredId flapped null between the
       last real move and the click). 'static' still hit-tests against
       CURRENT transforms on every real pointer event — the only loss is a
       node sliding out from under a perfectly still cursor keeping its
       highlight until the next move, which is exactly what Quartz does. */
    g.eventMode = 'static'
    g.cursor = 'pointer'
    /* explicit circle hit area: math test, not containsPoint over the
       graphics context — with thousands of nodes this is the difference
       between a free pointermove and a stutter */
    g.hitArea = new Circle(0, 0, nodeRadius(n) + 2)
    g.on('pointerover', () => {
      if (dragging) return
      hoveredId = n.idx
      applyHighlight(n.idx)
      hoverFocus.value = snapshot(n)
    })
    /* click-to-pin lives in d3-drag's end handler (pointer distance < 5px).
       NOT pixi pointertap: a dragged node travels WITH the cursor, so pixi
       sees down+up over the same object and fires tap after every drag —
       each drag ended with a phantom pin. d3's e.x/e.y measure the POINTER,
       which is immune to the node following it. */
    g.on('pointerleave', () => {
      hoveredId = null
      if (!dragging) {
        applyHighlight(null)
        hoverFocus.value = null
      }
    })
    nodeLayer.addChild(g)
    n.gfx = g
  }
  const byDegree = [...nodes].sort((a, b) => b.subDegree - a.subDegree).slice(0, LABEL_BUDGET)
  for (const n of byDegree) ensureLabel(n)

  simulation = forceSimulation<StarNode>(nodes)
    .force('collide', forceCollide<StarNode>((n) => nodeRadius(n))
      .iterations(N > 2500 ? 1 : 3))
  applyForces(1) /* the reference blooms from full alpha */

  const canvas = app.canvas as HTMLCanvasElement

  /* drag FIRST, zoom SECOND — attach order is the arbitration */
  let downX = 0, downY = 0
  /* Subject by PROXIMITY at mousedown, not by trusting the last pointerover.
     While the sim drifts (settle runs ~seconds at this node count), the
     node moves between the last hover event and the click; pixi's
     pointerdown then re-hit-tests empty sky and emits pointerleave BEFORE
     d3 sees the mousedown — hoveredId is null, no subject, no drag, no
     click. Measured as a nondeterministic click failure. Nearest-node
     within grab radius in world coords is immune to all of it. */
  const grabSubject = (e: any): StarNode | undefined => {
    const wx = (e.x - currentTransform.x) / currentTransform.k
    const wy = (e.y - currentTransform.y) / currentTransform.k
    let best: StarNode | undefined
    let bd = Infinity
    for (const n of nodes) {
      const dx = n.x! - wx, dy = n.y! - wy
      const d2 = dx * dx + dy * dy
      /* grab radius mirrors the pixi hitArea exactly: if a star isn't
         hoverable it must not be grabbable, or an empty-sky press silently
         drags an unseen node and the background pan never reaches d3-zoom */
      const r = nodeRadius(n) + 2
      if (d2 < r * r && d2 < bd) { best = n; bd = d2 }
    }
    return best
  }
  const dragB = drag<HTMLCanvasElement, unknown>()
    .container(() => canvas)
    .subject(grabSubject as any)
    .on('start', (e: any) => {
      if (!simulation) return
      /* grabbing IS hovering — restore the highlight the drift race may
         have just cleared */
      applyHighlight(e.subject.idx)
      hoverFocus.value = snapshot(e.subject)
      if (!e.active) simulation.alphaTarget(1).restart()
      e.subject.fx = e.subject.x
      e.subject.fy = e.subject.y
      e.subject.__initialDragPos = { x: e.subject.x, y: e.subject.y }
      downX = e.x; downY = e.y
      dragging = true
    })
    .on('drag', (e: any) => {
      const p = e.subject.__initialDragPos
      e.subject.fx = p.x + (e.x - downX) / currentTransform.k
      e.subject.fy = p.y + (e.y - downY) / currentTransform.k
    })
    .on('end', (e: any) => {
      dragging = false
      if (!simulation) return
      if (!e.active) simulation.alphaTarget(0)
      e.subject.fx = null
      e.subject.fy = null
      /* Click-vs-drag decided HERE, on POINTER distance — not via pixi
         pointertap. The grab reheats the sim (alphaTarget(1), as Obsidian
         does), so the node scoots out from under a still cursor before
         mouseup and pixi's tap never sees up over the same object. d3's
         e.x/e.y are pointer coords and immune to that. */
      const dist = Math.hypot(e.x - downX, e.y - downY)
      if (dist < 5) {
        pinnedFocus.value = snapshot(e.subject)
      } else if (hoveredId === null) {
        applyHighlight(null)
        hoverFocus.value = null
      }
    })
  select(canvas).call(dragB as any)

  zoomB = zoom<HTMLCanvasElement, unknown>()
    .scaleExtent([0.15, 6])
    .filter((e: any) => (!e.ctrlKey || e.type === 'wheel') && !e.button)
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

  /* ── the frame loop: strictly dirty-driven ── */
  app.ticker.add(() => {
    const now = performance.now()
    for (const group of tweens.values()) group.update(now)

    const simActive = !!simulation && simulation.alpha() > simulation.alphaMin()
    if (simActive || dragging) {
      syncNodeVisuals()
      writeLinkGeometry()
    }
    if (hoverAnimActive) {
      const t = Math.min(1, (now - hoverAnimStart) / HOVER_MS)
      /* quad ease-out — the closest curve to tween.js' default feel */
      const e = 1 - (1 - t) * (1 - t)
      for (let i = 0; i < nodeAlphaCur.length; i++) {
        nodeAlphaCur[i] = nodeAlphaFrom[i] + (nodeAlphaTo[i] - nodeAlphaFrom[i]) * e
        const g = nodes[i].gfx
        if (g) g.alpha = nodeAlphaCur[i]
      }
      for (let i = 0; i < linkAlphaCur.length; i++) {
        linkAlphaCur[i] = linkAlphaFrom[i] + (linkAlphaTo[i] - linkAlphaFrom[i]) * e
        linkLitCur[i] = linkLitFrom[i] + (linkLitTo[i] - linkLitFrom[i]) * e
      }
      writeLinkStyle()
      if (t >= 1) hoverAnimActive = false
    }
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
  buildGen++ /* invalidate any build still awaiting init */
  simulation?.stop()
  simulation = null
  tweens.forEach((g) => g.getAll().forEach((t) => t.stop()))
  tweens.clear()
  hoveredId = null
  highlightId = null
  dragging = false
  hoverAnimActive = false
  if (app) { app.destroy(true, { children: true }); app = null }
  stage = null; nodeLayer = null; labelLayer = null
  linkMesh = null; posBuf = null; styleBuf = null
  nodes = []; linkPairs = new Uint32Array(0); adjacency = []
  currentTransform = zoomIdentity
  ready.value = false
  hoverFocus.value = null
  pinnedFocus.value = null
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
  select(app.canvas as HTMLCanvasElement)
    .transition().duration(500)
    .call(
      zoomB.transform as any,
      zoomIdentity.translate(canvasHost.value.clientWidth / 2, canvasHost.value.clientHeight / 2),
    )
}

function seek() {
  const q = seekQuery.value.trim().toLowerCase()
  if (!q || !app || !zoomB || !canvasHost.value) return
  const hit = nodes.find((n) => n.name.toLowerCase().includes(q))
  if (!hit) return
  pinnedFocus.value = snapshot(hit)
  applyHighlight(hit.idx) /* highlight, never hoveredId — that belongs to the pointer */
  const host = canvasHost.value
  const k = Math.max(currentTransform.k, 1.8)
  select(app.canvas as HTMLCanvasElement)
    .transition().duration(650)
    .call(
      zoomB.transform as any,
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
.folio-close {
  position: absolute; top: 6px; right: 9px;
  background: none; border: none; cursor: pointer;
  color: rgba(238, 224, 196, 0.45); font-size: 15px; line-height: 1;
  font-family: inherit; padding: 2px;
}
.folio-close:hover { color: #e2bd7a; }
.webgl-note {
  position: absolute; top: 45%; left: 50%; transform: translate(-50%, -50%);
  color: rgba(238, 224, 196, 0.6); font-style: italic; font-size: 14px;
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
