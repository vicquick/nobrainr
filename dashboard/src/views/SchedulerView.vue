<template>
  <div class="horarium-page">
    <div class="horarium-shell">

      <!-- MASTHEAD -->
      <header class="horarium-masthead">
        <div class="masthead-rule" />
        <div class="masthead-inner">
          <div class="masthead-row">
            <span class="folio-label">Horarium · Daily Office</span>
            <span v-if="status" class="office-state" :class="{ atrest: !status.running }">
              <span class="state-mark">{{ status.running ? '✦' : '○' }}</span>
              {{ status.running ? 'observed' : 'at rest' }}
            </span>
          </div>
          <h1 class="horarium-title">The Hours</h1>
          <p class="horarium-tagline">
            Disciplines kept by the keepers of the book — the offices that maintain the corpus.
          </p>
        </div>
        <div class="masthead-rule" />
      </header>

      <template v-if="loading">
        <section class="quire quire-skel">
          <div class="quire-head">
            <span class="quire-numeral">I.</span>
            <span class="quire-title">Vitae · the corpus</span>
          </div>
          <FolioSkeleton :lines="3" :bars="2" caption="consulting the rule" />
        </section>
      </template>

      <template v-else>

        <!-- I — VITAL SIGNS -->
        <section v-if="health" class="quire">
          <div class="quire-head">
            <span class="quire-numeral">I.</span>
            <span class="quire-title">Vitae · the corpus</span>
          </div>

          <div class="ledger-figures cp-stagger">
            <RouterLink
              to="/memories"
              class="ledger-line ledger-link cp-clickable cp-marginalia"
              data-margin="[click] open the gathering"
              :style="staggerStyle(0)"
            >
              <span class="ledger-key">Memories scribed</span>
              <span class="ledger-fig">{{ memoriesScribed.toLocaleString() }}</span>
            </RouterLink>
            <RouterLink
              to="/graph"
              class="ledger-line ledger-link cp-clickable cp-marginalia"
              data-margin="[click] consult the dramatis personae"
              :style="staggerStyle(1)"
            >
              <span class="ledger-key">Entities catalogued</span>
              <span class="ledger-fig">{{ entitiesCatalogued.toLocaleString() }}</span>
            </RouterLink>
            <RouterLink
              to="/graph"
              class="ledger-line ledger-link cp-clickable cp-marginalia"
              data-margin="[click] follow the marginalia"
              :style="staggerStyle(2)"
            >
              <span class="ledger-key">Relations drawn</span>
              <span class="ledger-fig">{{ relationsDrawn.toLocaleString() }}</span>
            </RouterLink>
          </div>

          <!-- Extraction -->
          <div class="progress-block">
            <div class="progress-head">
              <span class="progress-label"><em>Entity extraction</em></span>
              <span class="progress-fig" :class="{ done: extractionPct >= 90 }">
                {{ extractionDone.toLocaleString() }} <em>of</em>
                {{ (health.extraction_done + health.extraction_pending)?.toLocaleString() }}
                · {{ extractionPct }}<span class="pct">%</span>
              </span>
            </div>
            <div class="folio-progress">
              <div class="folio-progress-bar" :style="{ width: extractionPct + '%' }" />
            </div>
            <p v-if="health.extraction_pending > 0" class="progress-tail">
              <em>{{ health.extraction_pending?.toLocaleString() }}</em> still awaiting the pen
            </p>
          </div>

          <!-- Quality -->
          <div class="progress-block">
            <div class="progress-head">
              <span class="progress-label"><em>Quality scoring</em></span>
              <span class="progress-fig" :class="{ done: qualityPct >= 90 }">
                {{ qualityScored.toLocaleString() }} <em>of</em>
                {{ (health.quality_scored + health.quality_unscored)?.toLocaleString() }}
                · {{ qualityPct }}<span class="pct">%</span>
              </span>
            </div>
            <div class="folio-progress">
              <div class="folio-progress-bar" :style="{ width: qualityPct + '%' }" />
            </div>
            <p v-if="health.quality_unscored > 0" class="progress-tail">
              <em>{{ health.quality_unscored?.toLocaleString() }}</em> awaiting judgment
            </p>
          </div>

          <p v-if="health.undistilled > 0" class="alert-line">
            <span class="alert-mark">⚠</span> <em>{{ health.undistilled }}</em>
            conversations remain undistilled.
          </p>
        </section>

        <!-- II — THE HOURS -->
        <section class="quire">
          <div class="quire-head">
            <span class="quire-numeral">II.</span>
            <span class="quire-title">Officium · the hours</span>
            <div class="office-controls">
              <button
                v-if="status?.running"
                class="folio-button"
                :disabled="actionLoading"
                @click="pauseScheduler"
              >suspend</button>
              <button
                v-else
                class="folio-button accent"
                :disabled="actionLoading"
                @click="resumeScheduler"
              >resume</button>
            </div>
          </div>

          <table v-if="status?.tasks.length" class="office-table">
            <thead>
              <tr>
                <th class="cp-marginalia" data-margin="the office's name in the rule">Discipline</th>
                <th class="cp-marginalia" data-margin="how often the keepers attend to it">Cadence</th>
                <th class="cp-marginalia" data-margin="when the office was last observed">Last observed</th>
                <th class="cp-marginalia" data-margin="how often this discipline has been kept">Times</th>
              </tr>
            </thead>
            <tbody class="cp-stagger">
              <tr
                v-for="(task, i) in status.tasks"
                :key="task.name"
                class="office-row office-row-clickable cp-marginalia"
                data-margin="[click] read this discipline's recent observances"
                :style="staggerStyle(i)"
                tabindex="0"
                role="button"
                :aria-label="`Open observances for ${task.name}`"
                @click="openTask(task.name)"
                @keydown.enter.prevent="openTask(task.name)"
                @keydown.space.prevent="openTask(task.name)"
              >
                <td>
                  <span class="task-name">{{ task.name }}</span>
                  <span v-if="task.type" class="task-type" :class="`type-${task.type}`">
                    {{ task.type }}
                  </span>
                </td>
                <td class="task-cadence">every {{ formatCadence(task.interval_hours) }}</td>
                <td class="task-last">{{ task.last_run ? formatRelative(task.last_run) : '—' }}</td>
                <td class="task-runs">{{ task.run_count.toLocaleString() }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-line">— no offices scheduled —</p>
        </section>

        <!-- III — FEEDBACK -->
        <section v-if="feedbackStats" class="quire">
          <div class="quire-head">
            <span class="quire-numeral">III.</span>
            <span class="quire-title">Of feedback received</span>
          </div>

          <div class="feedback-figures cp-stagger">
            <article
              class="ledger-line cp-marginalia"
              data-margin="every approval and rebuke recorded by the agent"
              :style="staggerStyle(0)"
            >
              <span class="ledger-key">Total responses</span>
              <span class="ledger-fig">{{ fbTotal.toLocaleString() }}</span>
            </article>
            <article
              class="ledger-line cp-marginalia"
              data-margin="answers the agent praised"
              :style="staggerStyle(1)"
            >
              <span class="ledger-key">Helpful · positive</span>
              <span class="ledger-fig accent-good">{{ fbPositive.toLocaleString() }}</span>
            </article>
            <article
              class="ledger-line cp-marginalia"
              data-margin="answers the agent set aside"
              :style="staggerStyle(2)"
            >
              <span class="ledger-key">Unhelpful · negative</span>
              <span class="ledger-fig accent-bad">{{ fbNegative.toLocaleString() }}</span>
            </article>
          </div>

          <div class="progress-block">
            <div class="progress-head">
              <span class="progress-label"><em>Positive ratio</em></span>
              <span class="progress-fig done">
                {{ (feedbackStats.positive_rate * 100).toFixed(0) }}<span class="pct">%</span>
              </span>
            </div>
            <div class="folio-progress">
              <div
                class="folio-progress-bar good"
                :style="{ width: (feedbackStats.positive_rate * 100) + '%' }"
              />
            </div>
          </div>
        </section>

        <!-- IV — RECENT EVENTS -->
        <section v-if="events.length" class="quire">
          <div class="quire-head">
            <span class="quire-numeral">IV.</span>
            <span class="quire-title">Annotationes · recent events</span>
            <span class="quire-flag">{{ events.length }} entries</span>
          </div>

          <ul class="events-list cp-stagger">
            <li v-for="(evt, i) in events" :key="evt.id" class="event-line" :style="staggerStyle(i)">
              <span class="event-type">{{ evt.event_type }}</span>
              <span class="event-source">{{ evt.source || '—' }}</span>
              <span class="event-data">{{ JSON.stringify(evt.event_data).slice(0, 90) }}</span>
              <span class="event-time">{{ formatRelative(evt.created_at) }}</span>
            </li>
          </ul>
        </section>

      </template>
    </div>

    <TaskDetailDrawer :task-name="drawerTask" @close="drawerTask = null" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useScheduler } from '@/composables/useScheduler'
import { useSSE } from '@/composables/useSSE'
import FolioSkeleton from '@/components/FolioSkeleton.vue'
import TaskDetailDrawer from '@/components/TaskDetailDrawer.vue'
import { useCountUp } from '@/composables/useCountUp'
import { staggerStyle } from '@/composables/useStaggerIndex'

const drawerTask = ref<string | null>(null)
function openTask(name: string) {
  drawerTask.value = name
}

const { status, events, feedbackStats, health, loading, actionLoading,
        fetchScheduler, pauseScheduler, resumeScheduler } = useScheduler()

// Vitae · the corpus — count-up tickers on first paint only.
const memoriesScribed   = useCountUp(computed(() => health.value?.total_memories  ?? 0))
const entitiesCatalogued = useCountUp(computed(() => health.value?.total_entities ?? 0))
const relationsDrawn    = useCountUp(computed(() => health.value?.total_relations ?? 0))
const extractionDone    = useCountUp(computed(() => health.value?.extraction_done ?? 0))
const qualityScored     = useCountUp(computed(() => health.value?.quality_scored  ?? 0))
const fbTotal    = useCountUp(computed(() => feedbackStats.value?.total    ?? 0))
const fbPositive = useCountUp(computed(() => feedbackStats.value?.positive ?? 0))
const fbNegative = useCountUp(computed(() => feedbackStats.value?.negative ?? 0))

const extractionPct = computed(() => {
  if (!health.value) return 0
  const total = health.value.extraction_done + health.value.extraction_pending
  return total > 0 ? Math.round((health.value.extraction_done / total) * 100) : 100
})

const qualityPct = computed(() => {
  if (!health.value) return 0
  const total = health.value.quality_scored + health.value.quality_unscored
  return total > 0 ? Math.round((health.value.quality_scored / total) * 100) : 100
})

function formatRelative(iso: string) {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function formatCadence(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)} minutes`
  if (hours === 1) return '1 hour'
  if (hours < 24) return `${hours} hours`
  if (hours === 24) return 'day'
  return `${Math.round(hours / 24)} days`
}

useSSE((evt) => {
  if (['agent_event', 'feedback_added'].includes(evt.type)) {
    fetchScheduler({ silent: true })
  }
})

onMounted(() => {
  fetchScheduler()
})
</script>

<style scoped>
.horarium-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  --cp-good: #8aa96e;
  --cp-bad: #c47a6a;
  --cp-warn: #c89e6e;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 32px 24px 80px;
  min-height: 100vh;
}

.horarium-shell {
  max-width: 920px;
  margin: 0 auto;
}

/* MASTHEAD */
.horarium-masthead { margin-bottom: 36px; }
.masthead-rule {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--cp-gold-soft) 30%, var(--cp-gold) 50%, var(--cp-gold-soft) 70%, transparent);
}
.masthead-inner { padding: 16px 0 12px; text-align: center; }
.masthead-row {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 11px; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--cp-ink-mute);
  margin-bottom: 12px;
}
.folio-label {
  font-style: italic; letter-spacing: 0.22em;
  color: var(--cp-gold); font-size: 10px;
}
.office-state {
  font-style: italic; font-size: 11px;
  letter-spacing: 0.05em; color: var(--cp-good);
  display: inline-flex; align-items: center; gap: 6px;
}
.office-state.atrest { color: var(--cp-ink-mute); }
.state-mark { font-size: 14px; }
.office-state.atrest .state-mark { color: var(--cp-gold-soft); }

.horarium-title {
  font-family: Georgia, serif;
  font-size: clamp(34px, 4.5vw, 48px);
  font-weight: 400; letter-spacing: 0.02em;
  margin: 0 0 4px; color: var(--cp-ink);
}
.horarium-tagline {
  font-style: italic; color: var(--cp-ink-mute);
  font-size: 14px; margin: 0 0 16px;
}

/* QUIRE */
.quire { margin-bottom: 56px; }
.quire-head {
  display: grid;
  grid-template-columns: 50px 1fr auto;
  gap: 12px; align-items: baseline;
  padding-bottom: 10px; margin-bottom: 18px;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.quire-numeral {
  font-family: Georgia, serif; font-size: 28px;
  color: var(--cp-gold); font-weight: 300;
  text-align: right; font-style: italic;
}
.quire-title {
  font-family: Georgia, serif; font-size: 18px;
  font-style: italic; color: var(--cp-ink);
  letter-spacing: 0.03em;
}
.quire-flag {
  font-family: Georgia, serif; font-style: italic;
  color: var(--cp-ink-mute); font-size: 13px;
}

/* OFFICE CONTROLS */
.office-controls { display: flex; gap: 8px; }
.folio-button {
  background: transparent;
  border: 1px solid var(--cp-gold-soft);
  color: var(--cp-gold);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  letter-spacing: 0.1em;
  padding: 4px 16px;
  cursor: pointer;
  transition: all 200ms;
}
.folio-button:hover:not(:disabled) {
  background: rgba(200, 169, 110, 0.08);
  border-color: var(--cp-gold);
}
.folio-button:disabled { opacity: 0.5; cursor: wait; }
.folio-button.accent {
  background: rgba(138, 169, 110, 0.1);
  border-color: var(--cp-good);
  color: var(--cp-good);
}
.folio-button.accent:hover:not(:disabled) {
  background: rgba(138, 169, 110, 0.18);
}

/* LEDGER FIGURES */
.ledger-figures, .feedback-figures {
  display: grid; gap: 0;
  margin-bottom: 24px;
}
.ledger-line {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: baseline;
  padding: 10px 8px 10px 64px;
  border-bottom: 1px dotted var(--cp-gold-faint);
}
.ledger-key {
  font-style: italic;
  font-size: 14px;
  color: var(--cp-ink-mute);
}
.ledger-fig {
  font-size: 22px;
  color: var(--cp-ink);
  font-variant-numeric: tabular-nums;
}
.ledger-fig.accent-good { color: var(--cp-good); }
.ledger-fig.accent-bad { color: var(--cp-bad); }

/* Ledger lines that route somewhere — cleanly inherit link color so
   the gold-on-parchment palette holds, and reinforce the clickable
   intent on hover (border-color shift, subtle lift via cp-clickable). */
.ledger-link {
  color: inherit;
  text-decoration: none;
}
.ledger-link:hover .ledger-key {
  color: var(--cp-gold-bright);
}
.ledger-link:hover .ledger-fig {
  color: var(--cp-gold-bright);
}

/* PROGRESS */
.progress-block { margin: 0 64px 24px; }
.progress-head {
  display: flex; justify-content: space-between;
  align-items: baseline; margin-bottom: 8px;
  font-family: Georgia, serif;
}
.progress-label {
  font-size: 13px; color: var(--cp-ink-mute);
  font-style: italic;
}
.progress-label em { color: var(--cp-ink); font-style: normal; }
.progress-fig {
  font-size: 13px; color: var(--cp-ink);
  font-variant-numeric: tabular-nums; font-style: italic;
}
.progress-fig em { color: var(--cp-ink-mute); font-style: italic; margin: 0 4px; }
.progress-fig.done { color: var(--cp-good); }
.pct { font-size: 11px; color: var(--cp-ink-mute); margin-left: 1px; }

.folio-progress {
  height: 5px;
  background: rgba(200, 169, 110, 0.08);
  border-top: 1px solid var(--cp-gold-faint);
  border-bottom: 1px solid var(--cp-gold-faint);
  overflow: hidden;
}
.folio-progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--cp-gold-soft), var(--cp-gold));
  transition: width 600ms cubic-bezier(0.22, 1, 0.36, 1);
}
.folio-progress-bar.good {
  background: linear-gradient(90deg, rgba(138, 169, 110, 0.5), var(--cp-good));
}
.progress-tail {
  font-family: Georgia, serif; font-style: italic;
  font-size: 12px; color: var(--cp-ink-mute);
  margin: 6px 0 0;
}
.progress-tail em { color: var(--cp-ink); font-style: normal; font-variant-numeric: tabular-nums; }

.alert-line {
  margin: 0 64px;
  font-family: Georgia, serif; font-style: italic;
  font-size: 13px; color: var(--cp-warn);
  padding: 8px 0;
}
.alert-mark { font-size: 14px; margin-right: 4px; }
.alert-line em { color: var(--cp-ink); font-style: normal; }

/* OFFICE TABLE — disciplines list */
.office-table {
  width: 100%;
  border-collapse: collapse;
  font-family: Georgia, serif;
  margin-top: 4px;
}
.office-table thead th {
  text-align: left;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-weight: 400;
  padding: 8px 12px;
  border-bottom: 1px solid var(--cp-gold-soft);
}
.office-table thead th:last-child { text-align: right; }
.office-row {
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    box-shadow var(--cp-dur-hover) var(--cp-ease);
}
.office-row:hover { background: rgba(200, 169, 110, 0.04); }
.office-row-clickable { cursor: pointer; }
.office-row-clickable:hover {
  background: rgba(200, 169, 110, 0.06);
  box-shadow: inset 2px 0 0 var(--cp-gold-soft);
}
.office-row-clickable:focus-visible {
  outline: none;
  box-shadow:
    inset 2px 0 0 var(--cp-gold-bright),
    0 0 0 2px rgba(200, 169, 110, 0.45);
}
.office-row td {
  padding: 12px;
  border-bottom: 1px dotted var(--cp-gold-faint);
  font-size: 13px;
  color: var(--cp-ink);
}
.office-row td:last-child { text-align: right; font-variant-numeric: tabular-nums; }

.task-name {
  font-family: Georgia, serif;
  letter-spacing: 0.02em;
  margin-right: 12px;
}
.task-type {
  font-size: 9px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-style: italic;
  padding: 2px 6px;
  background: rgba(200, 169, 110, 0.08);
  color: var(--cp-ink-mute);
}
.task-type.type-llm { color: var(--cp-warn); background: rgba(200, 158, 110, 0.1); }
.task-type.type-sql { color: var(--cp-good); background: rgba(138, 169, 110, 0.1); }
.task-type.type-system { color: var(--cp-gold); background: rgba(200, 169, 110, 0.1); }

.task-cadence, .task-last {
  font-style: italic;
  color: var(--cp-ink-mute);
  font-size: 13px;
}
.task-runs {
  color: var(--cp-gold);
  font-size: 13px;
}

/* EVENTS */
.events-list {
  list-style: none; padding: 0; margin: 0;
}
.event-line {
  display: grid;
  grid-template-columns: 100px 110px 1fr 80px;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px dotted var(--cp-gold-faint);
  align-items: baseline;
  font-family: Georgia, serif;
}
.event-type {
  font-size: 10px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-style: italic;
}
.event-source {
  font-size: 12px;
  color: var(--cp-ink-mute);
  font-style: italic;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.event-data {
  font-size: 12px;
  color: var(--cp-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.event-time {
  font-size: 11px;
  font-style: italic;
  color: var(--cp-ink-mute);
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}

/* STATES */
.horarium-loading, .empty-line {
  text-align: center;
  padding: 48px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
}
.loading-text { font-size: 13px; margin: 8px 0 0; letter-spacing: 0.05em; }
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }

/* MOBILE — kill fixed 64px paddings/margins, stack table-like grids */
@media (max-width: 720px) {
  .horarium-page { padding: 24px 14px 64px; }
  .horarium-title { font-size: 30px; }

  .quire { margin-bottom: 40px; }
  .quire-head {
    grid-template-columns: 36px 1fr auto;
    gap: 8px;
  }
  .quire-numeral { font-size: 22px; }
  .quire-title { font-size: 16px; }

  .ledger-line { padding: 10px 4px 10px 24px; }
  .ledger-fig { font-size: 18px; }
  .progress-block { margin: 0 0 24px; }
  .alert-line { margin: 0; }

  /* Office table: scroll horizontally if columns too tight,
     and shrink first-column max-width so long task names truncate */
  .office-table {
    display: block;
    overflow-x: auto;
    white-space: nowrap;
  }
  .office-row td:first-child .task-name {
    display: inline-block;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: middle;
  }

  /* Events log: stack each row vertically — kicker line, then meta */
  .event-line {
    grid-template-columns: 1fr auto;
    grid-template-areas:
      "type time"
      "src  src"
      "data data";
    row-gap: 4px;
  }
  .event-type { grid-area: type; }
  .event-time { grid-area: time; }
  .event-source { grid-area: src; }
  .event-data { grid-area: data; white-space: normal; }
}
</style>
