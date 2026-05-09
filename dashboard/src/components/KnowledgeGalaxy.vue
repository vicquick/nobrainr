<template>
  <v-theme-provider theme="dark">
  <div class="knowledge-galaxy" ref="containerRef">
    <!-- Controls overlay -->
    <div class="galaxy-controls" v-if="loaded">
      <v-btn-toggle v-model="colorMode" mandatory density="compact" variant="outlined" rounded="lg" class="mb-2">
        <v-btn value="category" size="x-small">Category</v-btn>
        <v-btn value="tier" size="x-small">Tier</v-btn>
        <v-btn value="importance" size="x-small">Importance</v-btn>
      </v-btn-toggle>
      <div class="galaxy-point-count">
        {{ pointCount.toLocaleString() }} memories
      </div>
    </div>

    <!-- Legend — click to toggle category visibility -->
    <div class="galaxy-legend" v-if="loaded && colorMode === 'category'">
      <div
        v-for="cat in sortedCategories"
        :key="cat.name"
        class="galaxy-legend-item"
        :class="{ 'galaxy-legend-hidden': !categoryVisible[cat.name] }"
        @click="toggleCategory(cat.name)"
      >
        <span class="galaxy-legend-dot" :style="{ background: cat.color }"></span>
        {{ cat.name }} <span class="galaxy-legend-count">({{ cat.count.toLocaleString() }})</span>
      </div>
    </div>

    <!-- Tier legend -->
    <div class="galaxy-legend" v-if="loaded && colorMode === 'tier'">
      <div v-for="t in tierLegend" :key="t.name" class="galaxy-legend-item"
        :class="{ 'galaxy-legend-hidden': !tierVisible[t.tier] }" @click="toggleTier(t.tier)">
        <span class="galaxy-legend-dot" :style="{ background: t.color }"></span>
        {{ t.name }} <span class="galaxy-legend-count">({{ t.count.toLocaleString() }})</span>
      </div>
    </div>

    <!-- Top-right controls -->
    <div class="galaxy-top-right" v-if="loaded">
      <v-btn
        icon size="small" variant="text"
        :aria-label="autoRotating ? 'Pause rotation' : 'Resume rotation'"
        :title="autoRotating ? 'Pause rotation' : 'Resume rotation'"
        @click="toggleAutoRotate"
      >
        <v-icon :icon="autoRotating ? 'mdi-pause' : 'mdi-play'" />
      </v-btn>
      <v-btn
        icon size="small" variant="text"
        :aria-label="isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen'"
        @click="toggleFullscreen"
      >
        <v-icon :icon="isFullscreen ? 'mdi-fullscreen-exit' : 'mdi-fullscreen'" />
      </v-btn>
    </div>

    <!-- Hover tooltip -->
    <div v-if="hoveredMemory" class="galaxy-tooltip"
      :style="{ left: tooltipPos.x + 'px', top: tooltipPos.y + 'px' }">
      <div class="galaxy-tooltip-cat">
        <span class="galaxy-legend-dot" :style="{ background: CATEGORY_COLORS[hoveredMemory.category] || '#888' }"></span>
        {{ hoveredMemory.category }}
      </div>
      <div class="galaxy-tooltip-text">{{ hoveredMemory.summary }}</div>
    </div>

    <!-- Detail panel on double-click -->
    <Transition name="galaxy-panel">
      <div v-if="selectedMemory" class="galaxy-detail-panel" @click.stop>
        <div class="galaxy-detail-header">
          <span class="text-body-1 font-weight-medium">Memory Detail</span>
          <v-btn icon size="x-small" variant="text" aria-label="Close memory detail" @click="selectedMemory = null">
            <v-icon icon="mdi-close" size="16" />
          </v-btn>
        </div>
        <div class="galaxy-detail-meta">
          <div class="galaxy-detail-row">
            <v-chip :color="CATEGORY_COLORS[selectedMemory.category]" size="x-small" variant="tonal">
              {{ selectedMemory.category }}
            </v-chip>
            <v-chip size="x-small" variant="outlined" class="ml-1">
              tier {{ selectedMemory.tier }}
            </v-chip>
          </div>
          <div class="galaxy-detail-content">{{ selectedMemory.summary }}</div>
          <div v-if="selectedMemory.id" class="galaxy-detail-row text-medium-emphasis" style="font-size: 11px;">
            {{ selectedMemory.id }}
          </div>
        </div>
      </div>
    </Transition>

    <!-- Loading state -->
    <div v-if="loading" class="galaxy-loading">
      <v-progress-circular indeterminate size="48" />
      <div class="text-body-medium mt-2">Computing knowledge galaxy...</div>
      <div class="text-body-small text-medium-emphasis mt-1">UMAP dimensionality reduction on {{ estimatedCount.toLocaleString() }} memories</div>
    </div>

    <!-- Three.js canvas -->
    <canvas ref="canvasRef" class="galaxy-canvas" v-show="loaded" role="img" aria-label="Knowledge Galaxy" />
  </div>
  </v-theme-provider>
</template>

<script setup lang="ts">
import { ref, reactive, computed, shallowRef, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import api from '@/api/client'

// ---------------------------------------------------------------------------
// Category colors — matches nobrainr canonical categories
// ---------------------------------------------------------------------------
const CATEGORY_COLORS: Record<string, string> = {
  architecture: '#4080ff',
  debugging: '#ff4040',
  deployment: '#ff8020',
  infrastructure: '#ff6600',
  patterns: '#a040ff',
  tooling: '#40cc40',
  security: '#ff2060',
  frontend: '#20cccc',
  backend: '#2080ff',
  data: '#ccaa20',
  business: '#cc60cc',
  documentation: '#8090a0',
  'session-log': '#606870',
  insight: '#ffcc00',
  _archived: '#404040',
  other: '#555555',
}

const TIER_COLORS: Record<number, string> = {
  0: '#ffcc00', // pinned
  1: '#ff6040', // hot
  2: '#4080ff', // standard
  3: '#404050', // cold
}

const COLOR_MODE_MAP: Record<string, number> = { category: 0, tier: 1, importance: 2 }

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const containerRef = ref<HTMLElement>()
const canvasRef = ref<HTMLCanvasElement>()

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const loading = ref(false)
const loaded = ref(false)
const colorMode = ref<'category' | 'tier' | 'importance'>('category')
const autoRotating = ref(true)
const isFullscreen = ref(false)
const pointCount = ref(0)
const estimatedCount = ref(32000)
const selectedMemory = ref<any>(null)

// Data arrays
const positionData = shallowRef<Float32Array>(new Float32Array(0))
const categoryData = shallowRef<string[]>([])
const tierData = shallowRef<number[]>([])
const importanceData = shallowRef<Float32Array>(new Float32Array(0))
const memoryIds = shallowRef<string[]>([])
const memorySummaries = shallowRef<string[]>([])

// Category visibility
const categoryVisible = reactive<Record<string, boolean>>({})

// Tier visibility
const tierVisible = reactive<Record<number, boolean>>({ 0: true, 1: true, 2: true, 3: true })

// Hover
const hoveredMemory = ref<{ id: string; category: string; summary: string } | null>(null)
const tooltipPos = ref({ x: 0, y: 0 })

// Category counts
const categoryCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const cat of categoryData.value) {
    counts[cat] = (counts[cat] || 0) + 1
  }
  return counts
})

const sortedCategories = computed(() => {
  return Object.entries(categoryCounts.value)
    .map(([name, count]) => ({ name, count, color: CATEGORY_COLORS[name] || '#555' }))
    .sort((a, b) => b.count - a.count)
})

const tierLegend = computed(() => {
  const counts: Record<number, number> = { 0: 0, 1: 0, 2: 0, 3: 0 }
  for (const t of tierData.value) counts[t] = (counts[t] || 0) + 1
  return [
    { tier: 0, name: 'Pinned', color: TIER_COLORS[0], count: counts[0] },
    { tier: 1, name: 'Hot', color: TIER_COLORS[1], count: counts[1] },
    { tier: 2, name: 'Standard', color: TIER_COLORS[2], count: counts[2] },
    { tier: 3, name: 'Cold', color: TIER_COLORS[3], count: counts[3] },
  ]
})

function toggleCategory(cat: string) {
  categoryVisible[cat] = !categoryVisible[cat]
  rebuildCategoryAttribute()
}

function toggleTier(tier: number) {
  tierVisible[tier] = !tierVisible[tier]
  rebuildCategoryAttribute()
}

// ---------------------------------------------------------------------------
// Three.js references
// ---------------------------------------------------------------------------
let THREE: any = null
let scene: any = null
let camera: any = null
let renderer: any = null
let pointsMesh: any = null
let orbitControls: any = null
let composer: any = null
let animationId: number | null = null

// GPU picking
let pickTarget: any = null
let pickMaterial: any = null
const pickPixel = new Uint8Array(4)
let pickDirty = false
let mouseCanvasX = 0
let mouseCanvasY = 0
let lastPickTime = 0
const PICK_THROTTLE_MS = 150

// ---------------------------------------------------------------------------
// Category → integer mapping for shader
// ---------------------------------------------------------------------------
const allCategories = Object.keys(CATEGORY_COLORS)

function catToIndex(cat: string): number {
  const idx = allCategories.indexOf(cat)
  return idx >= 0 ? idx : allCategories.length - 1
}

function catToColor(cat: string): [number, number, number] {
  const hex = CATEGORY_COLORS[cat] || '#555555'
  const r = parseInt(hex.slice(1, 3), 16) / 255
  const g = parseInt(hex.slice(3, 5), 16) / 255
  const b = parseInt(hex.slice(5, 7), 16) / 255
  return [r, g, b]
}

// ---------------------------------------------------------------------------
// Shaders — InstancedBufferGeometry billboard quads
// ---------------------------------------------------------------------------
const VISUAL_VERTEX = `
  attribute vec3 instancePosition;
  attribute vec3 instanceColor;
  attribute float instanceAlpha;

  uniform vec2 viewportSize;

  varying vec2 vUV;
  varying vec3 vColor;
  varying float vGlowIntensity;

  void main() {
    float sz = 3.0;

    // Billboard: project then offset quad corners
    vec4 mvPosition = modelViewMatrix * vec4(instancePosition, 1.0);
    gl_Position = projectionMatrix * mvPosition;

    float pixelSize = sz * (150.0 / -mvPosition.z);
    pixelSize = clamp(pixelSize, 0.5, 20.0);
    gl_Position.xy += position.xy * pixelSize * 2.0 / viewportSize * gl_Position.w;

    vUV = uv;
    vColor = instanceColor;
    vGlowIntensity = instanceAlpha;
  }
`

const VISUAL_FRAGMENT = `
  varying vec2 vUV;
  varying vec3 vColor;
  varying float vGlowIntensity;

  void main() {
    float dist = length(vUV - vec2(0.5));
    if (dist > 0.5) discard;

    // Star-like glow: bright core + soft halo
    float core = 1.0 - smoothstep(0.0, 0.12, dist);
    float halo = exp(-dist * 7.0);
    float glow = core * 0.65 + halo * 0.35;

    gl_FragColor = vec4(vColor * glow, glow * vGlowIntensity);
  }
`

const PICK_VERTEX = `
  attribute vec3 instancePosition;
  attribute float instanceIndex;
  attribute float instanceAlpha;

  uniform vec2 viewportSize;
  varying vec3 vPickColor;

  void main() {
    if (instanceAlpha < 0.01) {
      gl_Position = vec4(0.0, 0.0, -2.0, 1.0);
      vPickColor = vec3(0.0);
      return;
    }

    float idx = instanceIndex + 1.0;
    vPickColor = vec3(
      mod(idx, 256.0) / 255.0,
      mod(floor(idx / 256.0), 256.0) / 255.0,
      floor(idx / 65536.0) / 255.0
    );

    float sz = 4.0;
    vec4 mvPosition = modelViewMatrix * vec4(instancePosition, 1.0);
    gl_Position = projectionMatrix * mvPosition;
    float pixelSize = sz * (150.0 / -mvPosition.z);
    pixelSize = clamp(pixelSize, 0.5, 20.0);
    gl_Position.xy += position.xy * pixelSize * 2.0 / viewportSize * gl_Position.w;
  }
`

const PICK_FRAGMENT = `
  varying vec3 vPickColor;
  void main() {
    gl_FragColor = vec4(vPickColor, 1.0);
  }
`

// ---------------------------------------------------------------------------
// Build color + alpha attributes from current mode + visibility
// ---------------------------------------------------------------------------
function buildColorAlpha(): { colors: Float32Array; alphas: Float32Array } {
  const count = pointCount.value
  const colors = new Float32Array(count * 3)
  const alphas = new Float32Array(count)

  for (let i = 0; i < count; i++) {
    let r = 0.5, g = 0.5, b = 0.5, a = 0.5

    if (colorMode.value === 'category') {
      const cat = categoryData.value[i]
      const vis = categoryVisible[cat] !== false
      ;[r, g, b] = catToColor(cat)
      a = vis ? 0.6 : 0.0
    } else if (colorMode.value === 'tier') {
      const tier = tierData.value[i]
      const vis = tierVisible[tier] !== false
      const hex = TIER_COLORS[tier] || '#555'
      r = parseInt(hex.slice(1, 3), 16) / 255
      g = parseInt(hex.slice(3, 5), 16) / 255
      b = parseInt(hex.slice(5, 7), 16) / 255
      a = vis ? (tier === 0 ? 0.9 : tier === 1 ? 0.7 : tier === 2 ? 0.4 : 0.15) : 0.0
    } else {
      // importance mode — gradient from dim to bright
      const imp = importanceData.value[i] || 0.5
      r = 0.2 + imp * 0.8
      g = 0.4 + imp * 0.5
      b = 1.0 - imp * 0.3
      a = 0.1 + imp * 0.7
    }

    colors[i * 3] = r
    colors[i * 3 + 1] = g
    colors[i * 3 + 2] = b
    alphas[i] = a
  }

  return { colors, alphas }
}

function rebuildCategoryAttribute() {
  if (!pointsMesh || !THREE) return
  const { colors, alphas } = buildColorAlpha()
  pointsMesh.geometry.setAttribute('instanceColor', new THREE.InstancedBufferAttribute(colors, 3))
  pointsMesh.geometry.setAttribute('instanceAlpha', new THREE.InstancedBufferAttribute(alphas, 1))
}

// ---------------------------------------------------------------------------
// Fullscreen + auto-rotate
// ---------------------------------------------------------------------------
function toggleAutoRotate() {
  autoRotating.value = !autoRotating.value
  if (orbitControls) {
    orbitControls.autoRotate = autoRotating.value
    orbitControls.enabled = autoRotating.value
  }
}

function toggleFullscreen() {
  const el = containerRef.value
  if (!el) return
  if (!document.fullscreenElement) {
    el.requestFullscreen()
    isFullscreen.value = true
  } else {
    document.exitFullscreen()
    isFullscreen.value = false
  }
}

// ---------------------------------------------------------------------------
// Three.js setup
// ---------------------------------------------------------------------------
async function loadThree() {
  const threeModule = await import('three')
  THREE = threeModule
  const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js')
  const { UnrealBloomPass } = await import('three/examples/jsm/postprocessing/UnrealBloomPass.js')
  const { EffectComposer } = await import('three/examples/jsm/postprocessing/EffectComposer.js')
  const { RenderPass } = await import('three/examples/jsm/postprocessing/RenderPass.js')
  return { THREE: threeModule, OrbitControls, UnrealBloomPass, EffectComposer, RenderPass }
}

function initScene(deps: any) {
  const { THREE, OrbitControls, UnrealBloomPass, EffectComposer, RenderPass } = deps
  const canvas = canvasRef.value
  const container = containerRef.value
  if (!canvas || !container) return
  const width = container.clientWidth
  const height = container.clientHeight

  scene = new THREE.Scene()
  scene.background = new THREE.Color(0x000000)

  camera = new THREE.PerspectiveCamera(60, width / height, 0.001, 60)
  camera.position.set(0.9, 0.7, 0.9)

  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
  renderer.setSize(width, height)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))

  orbitControls = new OrbitControls(camera, renderer.domElement)
  orbitControls.enableDamping = true
  orbitControls.dampingFactor = 0.05
  orbitControls.autoRotate = true
  orbitControls.autoRotateSpeed = 0.3
  orbitControls.maxDistance = 50
  orbitControls.minDistance = 0.005
  orbitControls.zoomSpeed = 1.2

  pickTarget = new THREE.WebGLRenderTarget(Math.floor(width / 2), Math.floor(height / 2))
  pickTarget.texture.minFilter = THREE.NearestFilter
  pickTarget.texture.magFilter = THREE.NearestFilter

  // Bloom for nebula effect
  composer = new EffectComposer(renderer)
  composer.addPass(new RenderPass(scene, camera))
  const bloomPass = new UnrealBloomPass(
    new THREE.Vector2(Math.floor(width / 2), Math.floor(height / 2)),
    0.4, 0.2, 0.7,  // strength, radius, threshold
  )
  composer.addPass(bloomPass)
}

// ---------------------------------------------------------------------------
// Build instanced billboard points
// ---------------------------------------------------------------------------
function buildPoints() {
  if (!THREE || !scene) return

  if (pointsMesh) {
    scene.remove(pointsMesh)
    pointsMesh.geometry.dispose()
    if (pointsMesh.material) pointsMesh.material.dispose()
    if (pickMaterial) { pickMaterial.dispose(); pickMaterial = null }
  }
  if (renderer) renderer.renderLists.dispose()

  const count = pointCount.value
  if (count === 0) return

  const container = containerRef.value
  const width = container?.clientWidth || 800
  const height = container?.clientHeight || 600

  const { colors, alphas } = buildColorAlpha()
  const indexFloat = new Float32Array(count)
  for (let i = 0; i < count; i++) indexFloat[i] = i

  const baseQuad = new THREE.PlaneGeometry(1, 1)
  const geometry = new THREE.InstancedBufferGeometry()
  geometry.index = baseQuad.index
  geometry.setAttribute('position', baseQuad.getAttribute('position'))
  geometry.setAttribute('uv', baseQuad.getAttribute('uv'))

  geometry.setAttribute('instancePosition', new THREE.InstancedBufferAttribute(positionData.value, 3))
  geometry.setAttribute('instanceColor', new THREE.InstancedBufferAttribute(colors, 3))
  geometry.setAttribute('instanceAlpha', new THREE.InstancedBufferAttribute(alphas, 1))
  geometry.setAttribute('instanceIndex', new THREE.InstancedBufferAttribute(indexFloat, 1))

  const material = new THREE.ShaderMaterial({
    uniforms: {
      viewportSize: { value: new THREE.Vector2(width, height) },
    },
    vertexShader: VISUAL_VERTEX,
    fragmentShader: VISUAL_FRAGMENT,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })

  pickMaterial = new THREE.ShaderMaterial({
    uniforms: {
      viewportSize: { value: new THREE.Vector2(width, height) },
    },
    vertexShader: PICK_VERTEX,
    fragmentShader: PICK_FRAGMENT,
    blending: THREE.NoBlending,
    depthWrite: true,
  })

  pointsMesh = new THREE.Mesh(geometry, material)
  pointsMesh.frustumCulled = false
  scene.add(pointsMesh)
}

// ---------------------------------------------------------------------------
// GPU picking
// ---------------------------------------------------------------------------
function performPick() {
  if (!pickDirty || !pointsMesh || !pickMaterial || !renderer || !camera || !scene || !pickTarget || zoomAnim) return
  const now = performance.now()
  if (now - lastPickTime < PICK_THROTTLE_MS) return
  lastPickTime = now
  pickDirty = false

  const origMaterial = pointsMesh.material
  const origBackground = scene.background

  renderer.autoClear = true
  scene.background = new THREE.Color(0x000000)
  pointsMesh.material = pickMaterial
  renderer.setRenderTarget(pickTarget)
  renderer.clear(true, true, false)
  renderer.render(scene, camera)
  renderer.setRenderTarget(null)

  pointsMesh.material = origMaterial
  scene.background = origBackground

  const scaleX = pickTarget.width / renderer.domElement.clientWidth
  const scaleY = pickTarget.height / renderer.domElement.clientHeight
  const px = Math.round(mouseCanvasX * scaleX)
  const py = pickTarget.height - 1 - Math.round(mouseCanvasY * scaleY)

  renderer.readRenderTargetPixels(pickTarget, px, py, 1, 1, pickPixel)

  const raw = pickPixel[0] + pickPixel[1] * 256 + pickPixel[2] * 65536
  if (raw === 0) {
    hoveredMemory.value = null
    if (orbitControls && autoRotating.value) orbitControls.autoRotate = true
    return
  }

  const index = raw - 1
  if (index >= 0 && index < pointCount.value) {
    hoveredMemory.value = {
      id: memoryIds.value[index],
      category: categoryData.value[index],
      summary: memorySummaries.value[index],
    }
    if (orbitControls && autoRotating.value) orbitControls.autoRotate = false
  }
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
function onMouseMove(event: MouseEvent) {
  if (!canvasRef.value) return
  const rect = canvasRef.value.getBoundingClientRect()
  mouseCanvasX = event.clientX - rect.left
  mouseCanvasY = event.clientY - rect.top
  tooltipPos.value = { x: mouseCanvasX + 15, y: mouseCanvasY + 15 }
  pickDirty = true
}

let zoomAnim: { start: number; from: any; to: any; fromTarget: any; toTarget: any } | null = null
const ZOOM_DURATION = 600

function onDblClick() {
  if (!camera || !orbitControls) return

  if (!hoveredMemory.value) {
    selectedMemory.value = null
    const dir = new THREE.Vector3().subVectors(camera.position, orbitControls.target).normalize()
    zoomAnim = {
      start: performance.now(),
      from: camera.position.clone(),
      to: dir.multiplyScalar(3),
      fromTarget: orbitControls.target.clone(),
      toTarget: new THREE.Vector3(0, 0, 0),
    }
    return
  }

  // Show detail
  const idx = memoryIds.value.indexOf(hoveredMemory.value.id)
  if (idx >= 0) {
    selectedMemory.value = {
      id: memoryIds.value[idx],
      category: categoryData.value[idx],
      tier: tierData.value[idx],
      summary: memorySummaries.value[idx],
    }

    const tx = positionData.value[idx * 3]
    const ty = positionData.value[idx * 3 + 1]
    const tz = positionData.value[idx * 3 + 2]
    const target = new THREE.Vector3(tx, ty, tz)
    const dir = new THREE.Vector3().subVectors(camera.position, orbitControls.target).normalize()
    const destination = new THREE.Vector3().copy(target).add(dir.multiplyScalar(0.05))

    zoomAnim = {
      start: performance.now(),
      from: camera.position.clone(),
      to: destination,
      fromTarget: orbitControls.target.clone(),
      toTarget: target,
    }
  }
}

function updateZoomAnimation() {
  if (!zoomAnim || !camera || !orbitControls) return
  const t = Math.min((performance.now() - zoomAnim.start) / ZOOM_DURATION, 1)
  const ease = 1 - Math.pow(1 - t, 3)
  camera.position.lerpVectors(zoomAnim.from, zoomAnim.to, ease)
  orbitControls.target.lerpVectors(zoomAnim.fromTarget, zoomAnim.toTarget, ease)
  orbitControls.update()
  if (t >= 1) zoomAnim = null
}

function onResize() {
  if (!containerRef.value || !renderer || !camera) return
  const width = containerRef.value.clientWidth
  const height = containerRef.value.clientHeight
  camera.aspect = width / height
  camera.updateProjectionMatrix()
  renderer.setSize(width, height)
  composer?.setSize(width, height)
  if (pickTarget) pickTarget.setSize(Math.floor(width / 2), Math.floor(height / 2))
  if (pointsMesh?.material?.uniforms?.viewportSize) {
    pointsMesh.material.uniforms.viewportSize.value.set(width, height)
  }
  if (pickMaterial?.uniforms?.viewportSize) {
    pickMaterial.uniforms.viewportSize.value.set(width, height)
  }
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------
async function loadData() {
  loading.value = true
  try {
    const { data } = await api.get('/api/galaxy')

    if (data.count && data.count > 0) {
      pointCount.value = data.count
      positionData.value = new Float32Array(data.positions)
      categoryData.value = data.categories
      tierData.value = data.tiers
      importanceData.value = new Float32Array(data.importances)
      memoryIds.value = data.ids
      memorySummaries.value = data.summaries

      // Init category visibility
      for (const cat of new Set(data.categories)) {
        if (categoryVisible[cat] === undefined) categoryVisible[cat] = true
      }

      loaded.value = true
    }
  } catch (err) {
    console.error('Failed to load galaxy data:', err)
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
onMounted(async () => {
  await loadData()

  if (loaded.value) {
    await nextTick()
    const deps = await loadThree()
    initScene(deps)
    if (!renderer || !camera || !scene) return
    buildPoints()

    let isVisible = true
    let mouseOverCanvas = false
    let lastInteraction = performance.now()
    const IDLE_THRESHOLD = 3000

    const observer = new IntersectionObserver(([entry]) => {
      isVisible = entry.isIntersecting
    }, { threshold: 0.1 })
    observer.observe(canvasRef.value!)

    canvasRef.value?.addEventListener('mouseenter', () => { mouseOverCanvas = true })
    canvasRef.value?.addEventListener('mouseleave', () => { mouseOverCanvas = false; pickDirty = false })
    const markActive = () => { lastInteraction = performance.now() }
    canvasRef.value?.addEventListener('pointerdown', markActive)
    canvasRef.value?.addEventListener('wheel', markActive)

    let frameCount = 0
    function animate() {
      animationId = requestAnimationFrame(animate)
      if (!isVisible) return

      frameCount++
      const isIdle = !mouseOverCanvas && !zoomAnim && (performance.now() - lastInteraction > IDLE_THRESHOLD)
      if (isIdle && frameCount % 6 !== 0) return

      updateZoomAnimation()
      orbitControls?.update()
      if (mouseOverCanvas) performPick()
      composer?.render()
    }
    animate()

    canvasRef.value?.addEventListener('mousemove', onMouseMove)
    canvasRef.value?.addEventListener('dblclick', onDblClick)
    window.addEventListener('resize', onResize)
    document.addEventListener('fullscreenchange', () => {
      isFullscreen.value = !!document.fullscreenElement
      setTimeout(onResize, 100)
    })
  }
})

onBeforeUnmount(() => {
  if (animationId) cancelAnimationFrame(animationId)
  canvasRef.value?.removeEventListener('mousemove', onMouseMove)
  canvasRef.value?.removeEventListener('dblclick', onDblClick)
  window.removeEventListener('resize', onResize)
  if (composer) { composer.dispose?.(); composer = null }
  if (orbitControls) { orbitControls.dispose(); orbitControls = null }
  if (pickTarget) { pickTarget.dispose(); pickTarget = null }
  if (pickMaterial) { pickMaterial.dispose(); pickMaterial = null }
  if (pointsMesh) {
    pointsMesh.geometry.dispose()
    pointsMesh.material.dispose()
    pointsMesh = null
  }
  if (renderer) { renderer.dispose(); renderer = null }
  scene = null
  camera = null
})

// Rebuild colors when mode changes
watch(colorMode, () => {
  rebuildCategoryAttribute()
})
</script>

<style scoped>
.knowledge-galaxy {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 600px;
  border-radius: 12px;
  overflow: hidden;
  background: #000000;
}

.galaxy-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.knowledge-galaxy:fullscreen {
  height: 100vh;
  border-radius: 0;
}

/* The 3D scene itself stays Three's native color space (changing the
   Bloom + node hues would require shader work and risk killing the
   atmosphere). What we CAN do — and what this pass does — is align
   every overlay control, tooltip, and detail panel with the codex
   palette so the chrome reads as parchment marginalia hovering over
   the constellation, not as a separate UI dialect. */

.galaxy-top-right {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 10;
  display: flex;
  gap: 2px;
  color: var(--cp-ink-mute);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.galaxy-top-right .v-btn { color: var(--cp-ink-mute); }
.galaxy-top-right .v-btn:hover { color: var(--cp-gold); }

.galaxy-controls {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  pointer-events: auto;
  font-family: Georgia, serif;
}

.galaxy-point-count {
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--cp-ink-faint);
  font-variant-numeric: tabular-nums;
}

.galaxy-legend {
  position: absolute;
  bottom: 12px;
  left: 12px;
  z-index: 10;
  display: flex;
  flex-direction: column;
  gap: 4px;
  pointer-events: auto;
  cursor: pointer;
  max-height: 300px;
  overflow-y: auto;
  font-family: Georgia, serif;
}

.galaxy-legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--cp-ink-mute);
  transition: color var(--cp-dur-hover) var(--cp-ease);
  user-select: none;
}

.galaxy-legend-item:hover { color: var(--cp-ink); }
.galaxy-legend-hidden { opacity: 0.35; }
.galaxy-legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.galaxy-legend-count {
  color: var(--cp-gold-soft);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  font-style: normal;
}

.galaxy-tooltip {
  position: absolute;
  z-index: 20;
  background:
    radial-gradient(400px 300px at 50% 0%, rgba(200, 169, 110, 0.06), transparent 70%),
    linear-gradient(180deg, rgba(20, 17, 10, 0.94), rgba(14, 11, 6, 0.94));
  border: 1px solid var(--cp-rule);
  border-radius: 2px;
  padding: 8px 12px;
  pointer-events: none;
  max-width: 280px;
  backdrop-filter: blur(8px);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}

.galaxy-tooltip-cat {
  display: flex;
  align-items: center;
  gap: 6px;
  font-style: italic;
  font-size: 10.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  margin-bottom: 4px;
}

.galaxy-tooltip-text {
  font-size: 12.5px;
  color: var(--cp-ink);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}

.galaxy-detail-panel {
  position: absolute;
  top: 12px;
  right: 12px;
  bottom: 12px;
  width: 320px;
  z-index: 15;
  background:
    radial-gradient(700px 500px at 50% 0%, rgba(200, 169, 110, 0.05), transparent 65%),
    linear-gradient(180deg, var(--cp-paper) 0%, var(--cp-paper-deep) 100%);
  border: 1px solid var(--cp-rule);
  border-radius: 4px;
  padding: 16px;
  backdrop-filter: blur(12px);
  overflow-y: auto;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  box-shadow: -8px 0 32px rgba(0, 0, 0, 0.5);
}

.galaxy-detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  color: var(--cp-ink);
  font-variant: small-caps;
  letter-spacing: 0.02em;
}

.galaxy-detail-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
  letter-spacing: 0.04em;
}

.galaxy-detail-content {
  font-family: Georgia, serif;
  font-size: 13px;
  line-height: 1.55;
  color: var(--cp-ink);
  margin: 12px 0;
  white-space: pre-wrap;
}

.galaxy-loading {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--cp-ink-mute);
  z-index: 5;
  font-family: Georgia, serif;
  font-style: italic;
  letter-spacing: 0.05em;
}

/* Transitions — slide+fade on the detail panel, codex tokens. */
.galaxy-panel-enter-active {
  transition:
    opacity var(--cp-dur-panel) var(--cp-ease-decel),
    transform var(--cp-dur-panel) var(--cp-ease-decel);
}
.galaxy-panel-leave-active {
  transition:
    opacity var(--cp-dur-out) var(--cp-ease-accel),
    transform var(--cp-dur-out) var(--cp-ease-accel);
}
.galaxy-panel-enter-from { opacity: 0; transform: translateX(20px); }
.galaxy-panel-leave-to { opacity: 0; transform: translateX(20px); }
</style>