<template>
  <div class="ins-page">
    <div class="ins-shell">
      <!-- Folio masthead -->
      <header class="ins-masthead">
        <div class="masthead-rule" />
        <div class="masthead-row">
          <div class="masthead-mark">
            <span class="ornament">❦</span>
            <span class="folio-label">Liber Insigniorum</span>
          </div>
          <div class="masthead-meta">{{ insights.length }} entries · {{ since.toUpperCase() }}</div>
        </div>
        <h1 class="ins-title">Insights</h1>
        <p class="ins-tagline">Lessons distilled from the graph, gathered as a commonplace book of one's own thinking.</p>
        <div class="masthead-rule" />
      </header>

      <!-- Filters -->
      <div class="ins-controls">
        <div class="ctl-group">
          <span class="ctl-label">Window</span>
          <button
            v-for="opt in ['24h','7d','30d']"
            :key="opt"
            class="folio-pill"
            :class="{ active: since === opt }"
            @click="since = opt"
          >{{ opt }}</button>
        </div>
        <div class="ctl-group">
          <span class="ctl-label">Min confidence</span>
          <input
            type="range" min="0" max="1" step="0.05"
            v-model.number="minConfidence"
            class="folio-slider"
          />
          <span class="ctl-value">{{ Math.round(minConfidence * 100) }}%</span>
        </div>
      </div>

      <!-- Insight of the day — the "frontispiece" -->
      <article
        v-if="todayInsight"
        class="frontispiece"
        @click="openInsight(todayInsight)"
      >
        <div class="frontispiece-marginalia">
          <span class="frontispiece-numeral">I</span>
          <span class="frontispiece-tag">Today</span>
        </div>
        <div class="frontispiece-body">
          <p class="frontispiece-eyebrow">— Insight of the day —</p>
          <p v-if="todayInsight.summary && todayInsight.summary.length < 80" class="frontispiece-kicker">
            {{ todayInsight.summary.replace(/^Synthesis:\s*/i, '') }}
          </p>
          <p class="frontispiece-text">{{ trim(todayInsight.content || todayInsight.summary, 600) }}</p>
          <div class="frontispiece-meta">
            <span v-if="todayInsight.created_at">{{ formatDate(todayInsight.created_at) }}</span>
            <span v-if="todayInsight.confidence" class="dot">·</span>
            <span v-if="todayInsight.confidence">confidence {{ Math.round(todayInsight.confidence * 100) }}%</span>
            <span v-if="todayInsight.quality_score" class="dot">·</span>
            <span v-if="todayInsight.quality_score">quality {{ Math.round(todayInsight.quality_score * 100) }}%</span>
          </div>
        </div>
        <div class="frontispiece-arrow">→</div>
      </article>

      <div v-if="loading" class="ins-loading">
        <Dotty />
      </div>
      <div v-else-if="!insights.length" class="ins-empty">
        — Nothing yet for this window —
      </div>

      <!-- Folio entry list -->
      <div v-else class="folio-entries cp-stagger">
        <article
          v-for="(ins, idx) in insights"
          :key="ins.id"
          class="folio-entry"
          :style="staggerStyle(idx)"
          @click="openInsight(ins)"
        >
          <div class="entry-numeral">{{ toRoman(idx + 1) }}.</div>
          <div class="entry-body">
            <p v-if="ins.summary && ins.summary.length < 80" class="entry-kicker">
              {{ ins.summary.replace(/^Synthesis:\s*/i, '') }}
            </p>
            <p class="entry-text">{{ trim(ins.content || ins.summary, 500) }}</p>
            <div class="entry-meta">
              <span>{{ formatDate(ins.created_at) }}</span>
              <span v-if="ins.confidence" class="dot">·</span>
              <span v-if="ins.confidence">{{ Math.round(ins.confidence * 100) }}% conf.</span>
              <span v-if="ins.quality_score" class="dot">·</span>
              <span v-if="ins.quality_score">{{ Math.round(ins.quality_score * 100) }}% quality</span>
              <span v-for="t in (ins.tags || []).slice(0, 3)" :key="t">
                <span class="dot">·</span>
                <span class="tag-italic">{{ t }}</span>
              </span>
            </div>
          </div>
        </article>
      </div>
    </div>

    <!-- Detail dialog — full folio entry view -->
    <Teleport to="body">
      <div v-if="showDetail" class="folio-overlay" @click.self="showDetail = false">
        <div class="folio-page">
          <div class="page-header">
            <span class="ornament-sm">❦</span>
            <span class="page-kicker">From the commonplace</span>
            <button class="page-close" aria-label="Close insight" @click="showDetail = false">×</button>
          </div>

          <div class="page-content">
            <p class="page-body">{{ selected?.content || selected?.summary }}</p>

            <div class="page-meta-row">
              <span v-if="selected?.created_at" class="meta-chip">
                <em>scribed</em> {{ formatDate(selected.created_at) }}
              </span>
              <span v-if="selected?.confidence" class="meta-chip primary">
                confidence · {{ Math.round(selected.confidence * 100) }}%
              </span>
              <span v-if="selected?.quality_score" class="meta-chip">
                quality · {{ Math.round(selected.quality_score * 100) }}%
              </span>
              <span v-if="selected?.category" class="meta-chip">
                {{ selected.category }}
              </span>
              <span v-for="t in (selected?.tags || [])" :key="t" class="meta-chip outlined">
                {{ t }}
              </span>
            </div>

            <p v-if="sourceEntity" class="page-source">
              <em>Drawn from the entity</em> <strong>{{ sourceEntity }}</strong>.
            </p>

            <div class="page-divider">
              <span class="divider-glyph">— · —</span>
            </div>

            <h3 class="page-section-title">Marginalia · related memories</h3>

            <div v-if="loadingRelated" class="related-loading">
              <Dotty />
            </div>
            <p v-else-if="!related.length" class="related-empty">
              — None close enough to cite —
            </p>
            <div v-else class="related-list">
              <article
                v-for="(m, i) in related"
                :key="m.id"
                class="related-entry"
                @click="openMemory(m)"
              >
                <span class="related-numeral">{{ toRoman(i + 1).toLowerCase() }}.</span>
                <div class="related-body">
                  <p class="related-text">{{ trim(m.summary || m.content, 240) }}</p>
                  <div class="related-meta">
                    <em>{{ m.source_type }}</em>
                    <span class="dot">·</span>
                    <span>{{ formatDate(m.created_at) }}</span>
                    <span v-if="m.similarity" class="dot">·</span>
                    <span v-if="m.similarity">sim {{ m.similarity.toFixed(2) }}</span>
                  </div>
                </div>
                <span class="related-arrow">›</span>
              </article>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import Dotty from '@/components/Dotty.vue'
import { staggerStyle } from '@/composables/useStaggerIndex'

interface Insight {
  id: string
  content?: string
  summary?: string
  created_at?: string
  confidence?: number
  quality_score?: number
  tags?: string[]
  category?: string
}

const since = ref('7d')
const minConfidence = ref(0.5)
const insights = ref<Insight[]>([])
const todayInsight = ref<Insight | null>(null)
const loading = ref(false)

const showDetail = ref(false)
const selected = ref<any>(null)
const related = ref<any[]>([])
const loadingRelated = ref(false)
const sourceEntity = ref<string | null>(null)

async function loadInsights() {
  loading.value = true
  try {
    const r = await fetch(
      `/api/insights?since=${since.value}&min_confidence=${minConfidence.value}&limit=50`,
    )
    insights.value = await r.json()
  } finally {
    loading.value = false
  }
}

async function loadTodayInsight() {
  const r = await fetch('/api/insights/today')
  const d = await r.json()
  todayInsight.value = d.insight
}

async function openInsight(ins: any) {
  selected.value = ins
  showDetail.value = true
  related.value = []
  sourceEntity.value = null
  loadingRelated.value = true
  try {
    const detailR = await fetch(`/api/memories/${ins.id}`)
    if (detailR.ok) {
      const detail = await detailR.json()
      selected.value = { ...ins, ...detail.memory }
      sourceEntity.value = detail.memory?.metadata?.source_entity || null
    }
    fetch(`/api/memories/${ins.id}/click`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    }).catch(() => {})
    const text = (ins.summary || ins.content || '').slice(0, 400)
    if (text) {
      const r = await fetch(`/api/memories?q=${encodeURIComponent(text)}&limit=6`)
      if (r.ok) {
        const arr = await r.json()
        related.value = arr.filter((m: any) => m.id !== ins.id).slice(0, 5)
      }
    }
  } catch (e) {
    // ignore
  } finally {
    loadingRelated.value = false
  }
}

async function openMemory(m: any) {
  selected.value = m
  related.value = []
  sourceEntity.value = null
  loadingRelated.value = true
  try {
    const detailR = await fetch(`/api/memories/${m.id}`)
    if (detailR.ok) {
      const detail = await detailR.json()
      selected.value = { ...m, ...detail.memory }
      sourceEntity.value = detail.memory?.metadata?.source_entity || null
    }
    fetch(`/api/memories/${m.id}/click`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    }).catch(() => {})
    const text = (m.summary || m.content || '').slice(0, 400)
    if (text) {
      const r = await fetch(`/api/memories?q=${encodeURIComponent(text)}&limit=6`)
      if (r.ok) {
        const arr = await r.json()
        related.value = arr.filter((x: any) => x.id !== m.id).slice(0, 5)
      }
    }
  } finally {
    loadingRelated.value = false
  }
}

function trim(s: string | undefined | null, n: number) {
  if (!s) return ''
  const str = String(s)
  return str.length > n ? str.slice(0, n).replace(/\s+\S*$/, '') + '…' : str
}

function formatDate(s?: string) {
  if (!s) return ''
  return new Date(s).toLocaleDateString('en-GB', {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

function toRoman(n: number): string {
  const roman: [number, string][] = [
    [50, 'L'], [40, 'XL'], [10, 'X'], [9, 'IX'],
    [5, 'V'], [4, 'IV'], [1, 'I'],
  ]
  let out = ''
  for (const [val, sym] of roman) {
    while (n >= val) { out += sym; n -= val }
  }
  return out
}

watch([since, minConfidence], loadInsights)
onMounted(() => {
  loadInsights()
  loadTodayInsight()
})
</script>

<style scoped>
:root {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  --cp-bg: rgba(18, 14, 8, 0.4);
  --cp-bg-deep: rgba(14, 11, 6, 0.55);
}

.ins-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  --cp-bg: rgba(18, 14, 8, 0.4);
  --cp-bg-deep: rgba(14, 11, 6, 0.55);
  min-height: 100vh;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 32px 24px 80px;
}

.ins-shell {
  max-width: 760px;
  margin: 0 auto;
}

/* MASTHEAD */
.ins-masthead {
  margin-bottom: 32px;
  text-align: center;
}
.masthead-rule {
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    var(--cp-gold-soft) 30%,
    var(--cp-gold) 50%,
    var(--cp-gold-soft) 70%,
    transparent
  );
}
.masthead-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 4px;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
}
.masthead-mark {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ornament {
  color: var(--cp-gold);
  font-size: 16px;
}
.folio-label {
  font-style: italic;
  font-family: Georgia, serif;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-size: 10px;
}
.ins-title {
  font-family: Georgia, serif;
  font-size: clamp(36px, 5vw, 56px);
  font-weight: 400;
  letter-spacing: 0.02em;
  margin: 12px 0 6px;
  text-align: center;
  color: var(--cp-ink);
}
.ins-tagline {
  text-align: center;
  font-style: italic;
  color: var(--cp-ink-mute);
  font-size: 14px;
  margin: 0 0 16px;
  font-family: Georgia, serif;
}

/* CONTROLS */
.ins-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  justify-content: center;
  align-items: center;
  margin-bottom: 32px;
  padding: 12px 0;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.ctl-group {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ctl-label {
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
}
.folio-pill {
  font-family: Georgia, serif;
  font-size: 12px;
  padding: 4px 12px;
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink-mute);
  cursor: pointer;
  letter-spacing: 0.05em;
  transition: all 150ms;
}
.folio-pill:hover { border-color: var(--cp-gold-soft); color: var(--cp-ink); }
.folio-pill.active {
  background: var(--cp-gold-faint);
  border-color: var(--cp-gold);
  color: var(--cp-gold);
  font-style: italic;
}
.folio-slider {
  width: 140px;
  accent-color: var(--cp-gold);
}
.ctl-value {
  font-family: Georgia, serif;
  font-style: italic;
  color: var(--cp-gold);
  font-size: 13px;
  min-width: 36px;
}

/* FRONTISPIECE — insight of the day */
.frontispiece {
  display: grid;
  grid-template-columns: 80px 1fr 24px;
  gap: 16px;
  align-items: center;
  padding: 28px 24px;
  margin-bottom: 36px;
  background: var(--cp-bg-deep);
  border-top: 2px solid var(--cp-gold);
  border-bottom: 1px solid var(--cp-gold-faint);
  cursor: pointer;
  transition: all 200ms cubic-bezier(0.22, 1, 0.36, 1);
  position: relative;
}
.frontispiece:hover {
  background: rgba(20, 16, 8, 0.6);
  transform: translateX(2px);
}
.frontispiece-marginalia {
  text-align: center;
  border-right: 1px solid var(--cp-gold-faint);
  padding-right: 16px;
}
.frontispiece-numeral {
  display: block;
  font-size: 48px;
  font-family: Georgia, serif;
  font-weight: 300;
  color: var(--cp-gold);
  line-height: 1;
  letter-spacing: 0;
}
.frontispiece-tag {
  font-size: 9px;
  letter-spacing: 0.25em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
  font-style: italic;
}
.frontispiece-eyebrow {
  font-size: 11px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--cp-gold);
  margin: 0 0 10px;
  font-style: italic;
}
.frontispiece-kicker {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  letter-spacing: 0.06em;
  color: var(--cp-gold);
  margin: 0 0 8px;
  text-transform: lowercase;
}
.frontispiece-text {
  font-size: 17px;
  line-height: 1.65;
  color: var(--cp-ink);
  margin: 0 0 14px;
  font-family: Georgia, serif;
}
.frontispiece-meta {
  font-size: 12px;
  color: var(--cp-ink-mute);
  font-style: italic;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.dot { color: var(--cp-gold-soft); }
.frontispiece-arrow {
  color: var(--cp-gold);
  font-size: 22px;
  opacity: 0.6;
  transition: opacity 200ms, transform 200ms;
}
.frontispiece:hover .frontispiece-arrow {
  opacity: 1;
  transform: translateX(4px);
}

/* FOLIO ENTRY LIST */
.folio-entries {
  border-top: 1px solid var(--cp-gold-faint);
}
.folio-entry {
  display: grid;
  grid-template-columns: 60px 1fr;
  gap: 16px;
  padding: 22px 8px;
  border-bottom: 1px solid var(--cp-gold-faint);
  cursor: pointer;
  transition: all 150ms;
}
.folio-entry:hover {
  background: rgba(200, 169, 110, 0.04);
  padding-left: 14px;
}
.entry-numeral {
  font-family: Georgia, serif;
  font-size: 18px;
  color: var(--cp-gold);
  font-style: italic;
  text-align: right;
  padding-top: 2px;
  font-weight: 400;
  letter-spacing: 0.05em;
}
.entry-kicker {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--cp-gold);
  margin: 0 0 4px;
  text-transform: lowercase;
}
.entry-text {
  font-size: 15px;
  line-height: 1.65;
  margin: 0 0 8px;
  color: var(--cp-ink);
  font-family: Georgia, serif;
}
.entry-meta {
  font-size: 12px;
  color: var(--cp-ink-mute);
  font-style: italic;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.tag-italic { font-style: italic; color: var(--cp-gold-soft); }

/* STATES */
.ins-loading, .ins-empty, .related-loading, .related-empty {
  text-align: center;
  padding: 48px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
}
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }

/* DIALOG — folio page */
.folio-overlay {
  position: fixed;
  inset: 0;
  background: rgba(8, 6, 3, 0.85);
  backdrop-filter: blur(4px);
  z-index: 9999;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 40px 20px;
  overflow-y: auto;
}
.folio-page {
  max-width: 720px;
  width: 100%;
  background: linear-gradient(180deg, rgba(28, 22, 13, 0.98), rgba(18, 14, 8, 0.98));
  border: 1px solid var(--cp-gold-faint);
  border-top: 3px solid var(--cp-gold);
  padding: 0;
  box-shadow: 0 40px 80px rgba(0, 0, 0, 0.5), 0 0 0 1px var(--cp-gold-faint);
  font-family: Georgia, serif;
  animation: page-rise 280ms cubic-bezier(0.22, 1, 0.36, 1);
}
@keyframes page-rise {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}
.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 28px;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.ornament-sm { color: var(--cp-gold); font-size: 14px; }
.page-kicker {
  font-size: 10px;
  letter-spacing: 0.25em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-style: italic;
  flex-grow: 1;
}
.page-close {
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink-mute);
  font-size: 22px;
  width: 30px;
  height: 30px;
  cursor: pointer;
  font-family: Georgia, serif;
  line-height: 1;
  transition: all 150ms;
}
.page-close:hover { color: var(--cp-gold); border-color: var(--cp-gold); }
.page-content { padding: 28px 36px 36px; }
.page-body {
  font-size: 16px;
  line-height: 1.75;
  color: var(--cp-ink);
  margin: 0 0 24px;
  white-space: pre-wrap;
  font-family: Georgia, serif;
}
.page-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 18px;
}
.meta-chip {
  font-size: 11px;
  padding: 4px 10px;
  background: var(--cp-gold-faint);
  color: var(--cp-ink);
  letter-spacing: 0.05em;
  border: 1px solid transparent;
}
.meta-chip em { color: var(--cp-ink-mute); margin-right: 4px; }
.meta-chip.outlined {
  background: transparent;
  border: 1px solid var(--cp-gold-faint);
  color: var(--cp-ink-mute);
  font-style: italic;
}
.meta-chip.primary {
  background: rgba(200, 169, 110, 0.18);
  color: var(--cp-gold);
  border: 1px solid var(--cp-gold-soft);
}
.page-source {
  font-style: italic;
  color: var(--cp-ink-mute);
  font-size: 13px;
  margin: 0 0 16px;
  font-family: Georgia, serif;
}
.page-source strong { color: var(--cp-gold); font-style: normal; }
.page-divider {
  text-align: center;
  margin: 24px 0 16px;
  color: var(--cp-gold-soft);
  letter-spacing: 0.5em;
}
.page-section-title {
  font-family: Georgia, serif;
  font-size: 13px;
  font-weight: 400;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold);
  margin: 0 0 14px;
  font-style: italic;
}

/* RELATED LIST */
.related-list { display: flex; flex-direction: column; }
.related-entry {
  display: grid;
  grid-template-columns: 32px 1fr 16px;
  gap: 10px;
  padding: 14px 6px;
  border-bottom: 1px solid var(--cp-gold-faint);
  cursor: pointer;
  transition: all 150ms;
  align-items: center;
}
.related-entry:hover {
  background: rgba(200, 169, 110, 0.05);
  padding-left: 12px;
}
.related-entry:last-child { border-bottom: none; }
.related-numeral {
  font-family: Georgia, serif;
  font-style: italic;
  color: var(--cp-gold-soft);
  text-align: right;
  font-size: 13px;
}
.related-text {
  font-size: 13px;
  line-height: 1.55;
  color: var(--cp-ink);
  margin: 0 0 4px;
  font-family: Georgia, serif;
}
.related-meta {
  font-size: 11px;
  color: var(--cp-ink-mute);
  font-style: italic;
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  align-items: center;
}
.related-meta em { font-style: normal; color: var(--cp-gold-soft); }
.related-arrow {
  color: var(--cp-gold-soft);
  font-size: 18px;
  transition: all 150ms;
}
.related-entry:hover .related-arrow {
  color: var(--cp-gold);
  transform: translateX(2px);
}

@media (max-width: 720px) {
  .ins-page { padding: 24px 14px 64px; }
  .ins-title { font-size: 30px; }

  /* Frontispiece: drop the marginalia column on phones */
  .frontispiece {
    grid-template-columns: 1fr 18px;
    padding: 20px 14px;
  }
  .frontispiece-marginalia { display: none; }
  .frontispiece-text { font-size: 15px; }

  .folio-entry {
    grid-template-columns: 36px 1fr;
    gap: 10px;
    padding: 16px 4px;
  }

  .ins-controls {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
  }
  .ins-controls .ctl-group {
    flex-wrap: wrap;
    justify-content: center;
  }

  /* Folio dialog: full-bleed on mobile */
  .folio-overlay { padding: 0; }
  .folio-page { border-top-width: 2px; min-height: 100vh; }
  .page-content { padding: 20px 18px 32px; }
  .page-body { font-size: 15px; }
}
</style>
