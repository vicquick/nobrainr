<template>
  <Teleport to="body">
    <Transition name="onboarding-shell">
      <div v-if="visible" class="onboarding-shell" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
        <div class="onboarding-backdrop" @click="dismiss" />

        <!-- Caveat: motion-v reserved for future shared-element work; the
             3-card sequence here uses Vue's <Transition mode="out-in">
             so the welcome flow is reliable on cold first paint and
             doesn't pull in motion-v's runtime when we're already in
             the initial bundle. -->
        <Transition name="onboarding-card" mode="out-in">
          <article :key="step" class="onboarding-card">
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
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'

/**
 * First-visit onboarding — three short cards, motion-light, codex-voiced.
 *
 * Storage key bumps if the copy ever changes substantively (`v1` → `v2`)
 * so users who saw the old version see the new one once. Skip / Begin /
 * outside-click all set the latched key.
 */
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

const visible = ref(false)
const step = ref(0)

const card = computed(() => CARDS[step.value])
const ornament = computed(() => ORNAMENTS[step.value])
const eyebrow = computed(() => EYEBROWS[step.value])
const isLast = computed(() => step.value === CARDS.length - 1)

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
  } catch {
    // Storage disabled (Safari private mode etc.) — flow still works for
    // this session, it just won't be remembered. Acceptable degradation.
  }
  visible.value = false
}

onMounted(() => {
  let already = false
  try {
    already = localStorage.getItem(STORAGE_KEY) === '1'
  } catch { /* see dismiss() */ }
  if (!already) {
    // Defer one tick so the first route paints under the backdrop;
    // a cold-start flash of empty state behind the welcome card looks
    // worse than a 50ms wait.
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

/* Shell: gentle backdrop fade. */
.onboarding-shell-enter-active,
.onboarding-shell-leave-active {
  transition: opacity 220ms var(--cp-ease);
}
.onboarding-shell-enter-from,
.onboarding-shell-leave-to { opacity: 0; }

/* Card: out-in transition between steps — old card fades and recedes
   slightly, new card lifts in. Lighter than a slide carousel. */
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

@media (prefers-reduced-motion: reduce) {
  .onboarding-shell-enter-active,
  .onboarding-shell-leave-active,
  .onboarding-card-enter-active,
  .onboarding-card-leave-active { transition: none !important; }
  .onboarding-card-enter-from,
  .onboarding-card-leave-to { transform: none; }
}
</style>
