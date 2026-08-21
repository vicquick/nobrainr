<template>
  <div class="scr-page">
    <div class="scr-shell">

      <!-- MASTHEAD -->
      <header class="scr-masthead">
        <div class="masthead-rule" />
        <div class="masthead-inner">
          <div class="masthead-row">
            <span class="folio-label">Liber Scribarum · Roll of Scribes</span>
            <div class="live-indicator">
              <span class="live-pulse" :class="{ active: pulsing }">❦</span>
              <span class="live-cd">refresh in {{ countdown }}″</span>
            </div>
          </div>
          <h1 class="scr-title">The Scriptorium</h1>
          <p class="scr-tagline">
            Which hands are at which desk, and upon what manuscript — across every house.
          </p>
        </div>
        <div class="masthead-rule" />
      </header>

      <!-- LOADING -->
      <template v-if="loading">
        <div class="loading-folio">
          <p class="loading-text">calling the roll</p>
        </div>
      </template>

      <!-- EMPTY: teach, don't shrug -->
      <template v-else-if="!houses.length">
        <section class="vacant">
          <span class="vacant-glyph">❧</span>
          <p class="vacant-line">Every desk stands empty.</p>
          <p class="vacant-note">
            A scribe is counted present only while writing. The roll is called from the
            <em>UserPromptSubmit</em> hook, so a session sits unlisted the moment it falls
            quiet — an idle machine and a darkened one read alike here. Silence is the
            expected state, not a fault.
          </p>
        </section>
      </template>

      <template v-else>
        <!-- ONE QUIRE PER HOUSE (machine) -->
        <section
          v-for="(house, hi) in houses"
          :key="house.machine"
          class="quire"
        >
          <div class="quire-head">
            <span class="quire-numeral">{{ roman(hi + 1) }}.</span>
            <span class="quire-title">{{ house.machine }}</span>
            <span class="quire-flag" :class="{ active: house.freshest < 120 }">
              {{ house.scribes.length }}
              {{ house.scribes.length === 1 ? 'hand' : 'hands' }}
            </span>
          </div>

          <!-- each scribe = one ruled line in the ledger -->
          <article
            v-for="s in house.scribes"
            :key="s.agent"
            class="desk"
            :class="{ dimming: s.age_s > 240 }"
          >
            <div class="desk-rule" />
            <div class="desk-body">
              <header class="desk-head">
                <span class="desk-project">{{ projectOf(s.agent) }}</span>
                <span class="desk-age" :title="s.last_seen">{{ ago(s.age_s) }}</span>
              </header>

              <p v-if="s.task" class="desk-task">{{ tidy(s.task) }}</p>
              <p v-else class="desk-task muted">— no work declared —</p>

              <footer class="desk-foot">
                <span class="desk-sig">{{ s.agent }}</span>
                <span
                  v-if="projectOf(s.agent) === 'general'"
                  class="desk-caveat"
                  title="No tmux session, no git repository — the project could not be named."
                >unascribed</span>
              </footer>
            </div>
          </article>
        </section>

        <!-- COLOPHON -->
        <footer class="colophon">
          <div class="masthead-rule" />
          <p class="colophon-line">
            {{ total }} {{ total === 1 ? 'hand' : 'hands' }} across
            {{ houses.length }} {{ houses.length === 1 ? 'house' : 'houses' }} ·
            counted within {{ Math.round(WINDOW_S / 60) }} minutes
          </p>
        </footer>
      </template>

    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import api from '@/api/client'

/**
 * Presence is a LIVENESS view, not a history. The window is deliberately
 * generous (15 min) rather than the 5-min default used by agents_active:
 * a scribe mid-tool-call can go several minutes without submitting a
 * prompt, and vanishing from the roll mid-task reads as breakage when it
 * is only quiet. Erring long makes the page calm instead of flickery.
 */
const WINDOW_S = 900
const REFRESH_S = 20

interface Scribe {
  agent: string
  machine: string
  status: string
  task: string | null
  last_seen: string
  age_s: number
}
interface House {
  machine: string
  scribes: Scribe[]
  freshest: number
}

const scribes = ref<Scribe[]>([])
const loading = ref(true)
const pulsing = ref(false)
const countdown = ref(REFRESH_S)

const total = computed(() => scribes.value.length)

/** Group by machine; freshest house first, freshest scribe first within it. */
const houses = computed<House[]>(() => {
  const by = new Map<string, Scribe[]>()
  for (const s of scribes.value) {
    if (!by.has(s.machine)) by.set(s.machine, [])
    by.get(s.machine)!.push(s)
  }
  return [...by.entries()]
    .map(([machine, list]) => ({
      machine,
      scribes: [...list].sort((a, b) => a.age_s - b.age_s),
      freshest: Math.min(...list.map((s) => s.age_s)),
    }))
    .sort((a, b) => a.freshest - b.freshest)
})

/**
 * Agent ids are `<machine>:<project>` since 2026-08-21. Older beats used a
 * flat name (fable-main, infra), so fall back to the whole string rather
 * than rendering an empty cell for a legacy scribe.
 */
function projectOf(agent: string): string {
  const i = agent.indexOf(':')
  return i === -1 ? agent : agent.slice(i + 1)
}

/**
 * Task lines are verbatim prompt text, first 140 chars — so they can open
 * with a pasted file path or an @-upload. Strip that opening noise so the
 * roll reads as work rather than as paths.
 */
function tidy(task: string): string {
  return task
    .replace(/^@?['"]?\/\S+['"]?\s*/, '')
    .replace(/\s+/g, ' ')
    .trim() || task.trim()
}

function ago(s: number): string {
  if (s < 45) return 'at the desk'
  if (s < 120) return 'a minute since'
  if (s < 3600) return `${Math.round(s / 60)} minutes since`
  return `${Math.round(s / 360) / 10} hours since`
}

const NUMERALS = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII']
function roman(n: number): string {
  return NUMERALS[n - 1] ?? String(n)
}

async function fetchRoll() {
  try {
    const { data } = await api.get<{ active: Scribe[]; count: number }>(
      `/api/presence?window_s=${WINDOW_S}`,
    )
    scribes.value = data.active ?? []
    pulsing.value = true
    setTimeout(() => (pulsing.value = false), 900)
  } catch {
    // Leave the previous roll on screen. A failed poll is not evidence
    // that every scribe left — blanking the page would say exactly that.
  } finally {
    loading.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval> | null = null
let cdTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  fetchRoll()
  pollTimer = setInterval(() => {
    fetchRoll()
    countdown.value = REFRESH_S
  }, REFRESH_S * 1000)
  cdTimer = setInterval(() => {
    countdown.value = countdown.value > 0 ? countdown.value - 1 : REFRESH_S
  }, 1000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (cdTimer) clearInterval(cdTimer)
})
</script>

<style scoped>
.scr-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 32px 24px 80px;
  min-height: 100vh;
}
.scr-shell { max-width: 980px; margin: 0 auto; }

/* ── masthead ─────────────────────────────────────────── */
.scr-masthead { margin-bottom: 36px; }
.masthead-rule {
  height: 1px;
  background: linear-gradient(
    90deg, transparent, var(--cp-gold-soft) 30%,
    var(--cp-gold) 50%, var(--cp-gold-soft) 70%, transparent
  );
}
.masthead-inner { padding: 16px 0 12px; text-align: center; }
.masthead-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.folio-label {
  font-style: italic; letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--cp-gold); font-size: 10px;
}
.live-indicator { display: flex; align-items: center; gap: 8px; }
.live-pulse { color: var(--cp-gold-soft); font-size: 12px; transition: opacity 0.9s ease; opacity: 0.35; }
.live-pulse.active { opacity: 1; }
.live-cd { font-size: 10px; letter-spacing: 0.14em; color: var(--cp-ink-mute); font-style: italic; }
.scr-title {
  font-family: Georgia, serif; font-size: clamp(34px, 4.5vw, 48px);
  font-weight: 400; letter-spacing: 0.02em; margin: 0 0 4px; color: var(--cp-ink);
}
.scr-tagline {
  font-style: italic; font-size: 13px; color: var(--cp-ink-mute);
  margin: 0; letter-spacing: 0.02em;
}

/* ── quire = one machine ──────────────────────────────── */
.quire { margin-bottom: 52px; }
.quire-head {
  display: grid; grid-template-columns: 44px 1fr auto;
  align-items: baseline; gap: 12px;
  border-bottom: 1px solid var(--cp-gold-faint);
  padding-bottom: 8px; margin-bottom: 4px;
}
.quire-numeral {
  font-family: Georgia, serif; font-size: 28px; color: var(--cp-gold);
  font-weight: 300; text-align: right; font-style: italic;
}
.quire-title {
  font-size: 19px; letter-spacing: 0.05em; color: var(--cp-ink);
}
.quire-flag {
  font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--cp-ink-mute); font-style: italic;
}
.quire-flag.active { color: var(--cp-gold); }

/* ── desk = one scribe ────────────────────────────────── */
.desk {
  display: grid; grid-template-columns: 44px 1fr; gap: 12px;
  padding: 14px 0 12px 0;
  transition: opacity 420ms cubic-bezier(0.22, 1, 0.36, 1);
}
/* A quieting desk fades rather than disappears: presence decays, it does
   not switch off, and the page should say so. */
.desk.dimming { opacity: 0.52; }
.desk-rule {
  /* the ruled margin of a manuscript page */
  border-right: 1px solid var(--cp-gold-faint);
  margin-right: 0;
}
.desk-body { min-width: 0; }
.desk-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; margin-bottom: 5px;
}
.desk-project {
  font-size: 15px; letter-spacing: 0.06em; color: var(--cp-gold);
}
.desk-age {
  font-size: 10px; letter-spacing: 0.12em; font-style: italic;
  color: var(--cp-ink-mute); white-space: nowrap;
}
.desk-task {
  margin: 0 0 6px; font-size: 13.5px; line-height: 1.62;
  color: var(--cp-ink);
  /* two lines, then let it go — a task line is a glance, not a document */
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.desk-task.muted { color: var(--cp-ink-mute); font-style: italic; }
.desk-foot { display: flex; align-items: baseline; gap: 10px; }
.desk-sig {
  font-size: 10px; letter-spacing: 0.14em; color: rgba(238, 224, 196, 0.38);
}
.desk-caveat {
  font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase;
  font-style: italic; color: rgba(200, 169, 110, 0.5);
  border-bottom: 1px dotted rgba(200, 169, 110, 0.35); cursor: help;
}

/* ── states ───────────────────────────────────────────── */
.loading-folio { text-align: center; padding: 80px 0; }
.loading-text {
  font-style: italic; letter-spacing: 0.14em; color: var(--cp-ink-mute); font-size: 12px;
}
.vacant { text-align: center; padding: 64px 0 40px; max-width: 560px; margin: 0 auto; }
.vacant-glyph { font-size: 26px; color: var(--cp-gold-soft); display: block; margin-bottom: 14px; }
.vacant-line { font-size: 17px; margin: 0 0 12px; color: var(--cp-ink); letter-spacing: 0.03em; }
.vacant-note {
  font-size: 12.5px; line-height: 1.75; color: var(--cp-ink-mute);
  font-style: italic; margin: 0;
}
.vacant-note em { color: var(--cp-gold-soft); font-style: normal; }

/* ── colophon ─────────────────────────────────────────── */
.colophon { margin-top: 8px; }
.colophon-line {
  text-align: center; padding-top: 12px; margin: 0;
  font-size: 10px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--cp-ink-mute); font-style: italic;
}

@media (max-width: 600px) {
  .quire-head { grid-template-columns: 30px 1fr auto; gap: 8px; }
  .quire-numeral { font-size: 21px; }
  .desk { grid-template-columns: 30px 1fr; gap: 8px; }
  .desk-head { flex-direction: column; gap: 2px; }
}
@media (prefers-reduced-motion: reduce) {
  .desk, .live-pulse { transition: none; }
}
</style>
