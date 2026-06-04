<template>
  <Teleport to="body">
    <!-- Welcome modal -->
    <Transition name="onboarding-shell">
      <div v-if="visible" class="onboarding-shell" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
        <div class="onboarding-backdrop" />

        <Transition name="onboarding-card" mode="out-in">
          <article :key="step" class="onboarding-card">
            <!-- Close X -->
            <button class="onboarding-close" @click="dismiss" aria-label="Close">
              <span aria-hidden="true">✕</span>
            </button>

            <span class="onboarding-ornament" aria-hidden="true">{{ ornament }}</span>
            <p class="onboarding-eyebrow">{{ eyebrow }}</p>
            <h2 id="onboarding-title" class="onboarding-title">{{ card.title }}</h2>
            <p class="onboarding-body">{{ card.body }}</p>

            <div class="onboarding-pips" aria-hidden="true">
              <span
                v-for="(_, i) in CARDS"
                :key="i"
                class="onboarding-pip"
                :class="{ active: i === step }"
              />
            </div>

            <div class="onboarding-actions">
              <button class="onboarding-skip" @click="dismiss">Skip</button>
              <button class="onboarding-next" @click="advance">
                {{ isLast ? 'Begin' : 'Next →' }}
              </button>
            </div>
          </article>
        </Transition>
      </div>
    </Transition>

    <!-- Floating codex trigger — only on main pages -->
    <Transition name="codex-fab">
      <button
        v-if="showFab"
        class="codex-fab"
        @click="reopen"
        aria-label="Open welcome guide"
        title="Open welcome guide"
      >
        <span class="codex-fab-ornament" aria-hidden="true">❦</span>
      </button>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const STORAGE_KEY = 'nobrainr.onboarded.v1'

interface Card { title: string; body: string }

const CARDS: Card[] = [
  {
    title: 'Welcome to your codex',
    body:
      "nobrainr is a commonplace book stitched into a knowledge graph — " +
      "memories, the entities they speak of, and the marginalia connecting " +
      "one entry to another. It remembers what you've learned so future " +
      "sessions can build on it instead of starting blind.",
  },
  {
    title: 'Begin in the Commonplace',
    body:
      "The book gathers your notes into chapters by theme. Each chapter " +
      "is a community of memories the synthesizer scribe has woven " +
      "together. Open the Commonplace tab and pick a chapter, or jump " +
      "straight to the Memories index to read entries directly.",
  },
  {
    title: 'Ask the agent, any time',
    body:
      "The chat panel at the top-right has the whole graph as context. " +
      "Try \"what did I learn about X\" or \"summarize threads from last week.\" " +
      "Answers cite the memories they came from so you can verify.",
  },
]

const ORNAMENTS = ['❦', '✦', '⸻']
const EYEBROWS = ['Codex · I.', 'Codex · II.', 'Codex · III.']

// Only hide FAB on true detail/sub-pages (thread detail view)
const DEEP_ROUTES = new Set(['thread-detail'])

const route = useRoute()
const visible = ref(false)
const step = ref(0)
// Whether the user has ever dismissed, so we can show the FAB
const hasOnboarded = ref(false)

const card = computed(() => CARDS[step.value])
const ornament = computed(() => ORNAMENTS[step.value])
const eyebrow = computed(() => EYEBROWS[step.value])
const isLast = computed(() => step.value === CARDS.length - 1)

// FAB shows only on main pages and only after the modal has been dismissed at least once
const showFab = computed(
  () => hasOnboarded.value && !visible.value && !DEEP_ROUTES.has(String(route.name))
)

function advance() {
  if (isLast.value) {
    dismiss()
    return
  }
  step.value += 1
}

function dismiss() {
  try {
    localStorage.setItem(STORAGE_KEY, '1')
  } catch { /* Safari private mode etc. */ }
  hasOnboarded.value = true
  visible.value = false
}

function reopen() {
  step.value = 0
  visible.value = true
}

onMounted(() => {
  let already = false
  try {
    already = localStorage.getItem(STORAGE_KEY) === '1'
  } catch { /* see dismiss() */ }
  hasOnboarded.value = already
  if (!already) {
    setTimeout(() => { visible.value = true }, 50)
  }

  function onKey(e: KeyboardEvent) {
    if (!visible.value) return
    if (e.key === 'Escape') {
      dismiss()
    } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
      advance()
    } else if (e.key === 'ArrowLeft') {
      if (step.value > 0) step.value -= 1
    }
  }
  window.addEventListener('keydown', onKey)
})
</script>

<style scoped>
/* ── Modal shell ─────────────────────────────────────────────── */
.onboarding-shell {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: grid;
  place-items: center;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.onboarding-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(8, 6, 2, 0.62);
  backdrop-filter: blur(6px);
}

/* ── Card ────────────────────────────────────────────────────── */
.onboarding-card {
  position: relative;
  width: min(440px, 92vw);
  padding: 36px 32px 28px;
  background:
    radial-gradient(900px 600px at 50% 0%, rgba(200, 169, 110, 0.05), transparent 60%),
    linear-gradient(180deg, var(--cp-paper) 0%, var(--cp-paper-deep) 100%);
  border: 1px solid var(--cp-rule);
  border-radius: 4px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55);
  color: var(--cp-ink);
  text-align: center;
}

/* ── Close × ─────────────────────────────────────────────────── */
.onboarding-close {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 3px;
  cursor: pointer;
  color: var(--cp-ink-faint);
  font-size: 13px;
  letter-spacing: 0;
  font-family: inherit;
  transition:
    color var(--cp-dur-hover) var(--cp-ease),
    border-color var(--cp-dur-hover) var(--cp-ease),
    background var(--cp-dur-hover) var(--cp-ease);
  line-height: 1;
  padding: 0;
}
.onboarding-close:hover {
  color: var(--cp-ink);
  border-color: var(--cp-rule);
  background: rgba(200, 169, 110, 0.05);
}
.onboarding-close:focus-visible {
  box-shadow: var(--cp-focus-ring);
  outline: none;
}

/* ── Card content ────────────────────────────────────────────── */
.onboarding-ornament {
  display: block;
  font-size: 22px;
  color: var(--cp-gold);
  letter-spacing: 0.1em;
  margin-bottom: 8px;
}
.onboarding-eyebrow {
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  margin: 0 0 10px;
}
.onboarding-title {
  font-size: 24px;
  font-weight: 400;
  margin: 0 0 14px;
  font-variant: small-caps;
  letter-spacing: -0.005em;
}
.onboarding-body {
  font-size: 15px;
  line-height: 1.55;
  color: var(--cp-ink-mute);
  max-width: 36ch;
  margin: 0 auto 22px;
}

/* ── Pips ────────────────────────────────────────────────────── */
.onboarding-pips {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin: 0 0 22px;
}
.onboarding-pip {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(200, 169, 110, 0.20);
  transition: background var(--cp-dur-hover) var(--cp-ease);
}
.onboarding-pip.active { background: var(--cp-gold); }

/* ── Actions ─────────────────────────────────────────────────── */
.onboarding-actions {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.onboarding-skip,
.onboarding-next {
  font-family: inherit;
  font-size: 13px;
  letter-spacing: 0.05em;
  background: transparent;
  border: 1px solid var(--cp-rule);
  border-radius: 2px;
  padding: 8px 18px;
  cursor: pointer;
  color: var(--cp-ink-mute);
  transition:
    color var(--cp-dur-hover) var(--cp-ease),
    border-color var(--cp-dur-hover) var(--cp-ease),
    background var(--cp-dur-hover) var(--cp-ease);
}
.onboarding-skip:hover { color: var(--cp-ink); }
.onboarding-next {
  color: var(--cp-gold-bright);
  border-color: var(--cp-gold-soft);
}
.onboarding-next:hover {
  background: rgba(200, 169, 110, 0.08);
  color: var(--cp-gold-bright);
  border-color: var(--cp-gold-bright);
}

/* ── Floating codex FAB ──────────────────────────────────────── */
.codex-fab {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 190;
  width: 42px;
  height: 42px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(14, 11, 6, 0.82);
  border: 1px solid rgba(200, 169, 110, 0.22);
  border-radius: 50%;
  cursor: pointer;
  padding: 0;
  box-shadow:
    0 2px 12px rgba(0, 0, 0, 0.45),
    0 0 0 1px rgba(200, 169, 110, 0.04) inset;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  transition:
    border-color var(--cp-dur-hover) var(--cp-ease),
    box-shadow var(--cp-dur-hover) var(--cp-ease),
    background var(--cp-dur-hover) var(--cp-ease),
    transform var(--cp-dur-hover) var(--cp-ease-decel);
}
.codex-fab:hover {
  border-color: rgba(200, 169, 110, 0.50);
  background: rgba(18, 14, 8, 0.92);
  box-shadow:
    0 4px 20px rgba(0, 0, 0, 0.55),
    0 0 16px rgba(200, 169, 110, 0.10),
    0 0 0 1px rgba(200, 169, 110, 0.08) inset;
  transform: translateY(-1px);
}
.codex-fab:focus-visible {
  box-shadow: var(--cp-focus-ring);
  outline: none;
}
.codex-fab:active {
  transform: translateY(0);
  transition-duration: 80ms;
}

.codex-fab-ornament {
  font-size: 18px;
  color: rgba(200, 169, 110, 0.55);
  line-height: 1;
  display: block;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  transition: color var(--cp-dur-hover) var(--cp-ease);
  user-select: none;
}
.codex-fab:hover .codex-fab-ornament {
  color: rgba(200, 169, 110, 0.85);
}

/* ── Shell fade transition ───────────────────────────────────── */
.onboarding-shell-enter-active,
.onboarding-shell-leave-active {
  transition: opacity 220ms var(--cp-ease);
}
.onboarding-shell-enter-from,
.onboarding-shell-leave-to { opacity: 0; }

/* ── Card step transition ────────────────────────────────────── */
.onboarding-card-enter-active,
.onboarding-card-leave-active {
  transition:
    opacity 220ms var(--cp-ease),
    transform 220ms var(--cp-ease-decel);
}
.onboarding-card-enter-from {
  opacity: 0;
  transform: translateY(6px) scale(0.985);
}
.onboarding-card-leave-to {
  opacity: 0;
  transform: translateY(-4px) scale(0.99);
}

/* ── FAB fade transition ─────────────────────────────────────── */
.codex-fab-enter-active,
.codex-fab-leave-active {
  transition:
    opacity 200ms var(--cp-ease),
    transform 200ms var(--cp-ease-decel);
}
.codex-fab-enter-from,
.codex-fab-leave-to {
  opacity: 0;
  transform: translateY(6px) scale(0.9);
}

@media (prefers-reduced-motion: reduce) {
  .onboarding-shell-enter-active,
  .onboarding-shell-leave-active,
  .onboarding-card-enter-active,
  .onboarding-card-leave-active,
  .codex-fab-enter-active,
  .codex-fab-leave-active { transition: none !important; }
  .onboarding-card-enter-from,
  .onboarding-card-leave-to,
  .codex-fab-enter-from,
  .codex-fab-leave-to { transform: none; }
}
</style>
