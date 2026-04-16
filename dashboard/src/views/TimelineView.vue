<template>
  <v-container fluid style="max-width: 900px;">
    <!-- Filter Bar -->
    <div class="d-flex ga-3 mb-5 align-center">
      <v-select
        v-model="categoryFilter"
        :items="categoryOptions"
        label="Category"
        clearable
        style="max-width: 200px;"
      />
      <v-select
        v-model="machineFilter"
        :items="machineOptions"
        label="Machine"
        clearable
        style="max-width: 200px;"
      />
    </div>

    <!-- Loading -->
    <template v-if="loading">
      <div v-for="n in 3" :key="n" class="skeleton-block mb-4" />
    </template>

    <!-- Timeline -->
    <template v-else-if="groupedMemories.length">
      <div v-for="group in groupedMemories" :key="group.date" class="mb-8">
        <div class="d-flex align-center mb-4">
          <div class="timeline-date-pill">{{ group.date }}</div>
          <div class="timeline-line" />
        </div>

        <div class="timeline-entries">
          <TransitionGroup name="crystallize">
          <div
            v-for="mem in group.items"
            :key="mem.id"
            class="timeline-entry d-flex ga-3 mb-3"
            :class="{
              'timeline-entry--ghost': mem.queue_status === 'pending',
              'timeline-entry--rejected': mem.queue_status === 'failed',
            }"
          >
            <!-- Time -->
            <div
              class="text-caption pt-1"
              :class="mem.queue_status ? 'text-warning' : 'text-medium-emphasis'"
              style="min-width: 56px; font-variant-numeric: tabular-nums;"
            >
              <span v-if="mem.queue_status === 'pending'" class="ghost-tilde">~</span>
              <span v-if="mem.queue_status === 'failed'" class="ghost-tilde">×</span>
              {{ new Date(mem.created_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) }}
            </div>

            <!-- Dot + line -->
            <div class="d-flex flex-column align-center" style="width: 12px;">
              <div
                class="timeline-dot"
                :class="{
                  'timeline-dot--pending': mem.queue_status === 'pending',
                  'timeline-dot--failed': mem.queue_status === 'failed',
                }"
              />
              <div
                class="timeline-stem"
                :class="{ 'timeline-stem--flow': mem.queue_status === 'pending' }"
              />
            </div>

            <!-- Card -->
            <v-card variant="flat" class="flex-grow-1 timeline-card" :class="{
              'timeline-card--ghost': mem.queue_status === 'pending',
              'timeline-card--rejected': mem.queue_status === 'failed',
            }">
              <v-card-text class="pa-3">
                <div
                  class="text-body-2 font-weight-medium mb-1"
                  :class="{ 'ghost-text': mem.queue_status }"
                  style="line-height: 1.5;"
                >
                  {{ mem.summary || mem.content.slice(0, 200) + (mem.content.length > 200 ? '...' : '') }}
                </div>
                <div
                  v-if="mem.summary"
                  class="text-body-2 text-medium-emphasis mb-2"
                  :class="{ 'ghost-text': mem.queue_status }"
                  style="line-height: 1.5;"
                >
                  {{ mem.content.slice(0, 180) }}{{ mem.content.length > 180 ? '...' : '' }}
                </div>
                <div class="d-flex ga-2 align-center flex-wrap">
                  <v-chip v-if="mem.category" size="x-small" variant="tonal" color="primary" class="font-weight-medium">
                    {{ mem.category }}
                  </v-chip>
                  <v-chip v-if="mem.source_machine" size="x-small" variant="tonal" color="secondary" class="font-weight-medium">
                    {{ mem.source_machine }}
                  </v-chip>

                  <!-- Queue status marker: typographic, not a chip -->
                  <span
                    v-if="mem.queue_status === 'pending'"
                    class="queue-marker queue-marker--pending"
                    :title="`Awaiting extraction${mem.attempts ? ` (retry ${mem.attempts}/${mem.max_attempts})` : ''}`"
                  >
                    <span class="queue-marker__dots"><i /><i /><i /></span>
                    drafting
                  </span>
                  <span
                    v-else-if="mem.queue_status === 'failed'"
                    class="queue-marker queue-marker--failed"
                    :title="mem.error_message || 'Write failed'"
                  >
                    rejected · {{ mem.attempts }}/{{ mem.max_attempts }}
                  </span>

                  <v-spacer />
                  <div v-if="!mem.queue_status && mem.importance > 0" class="d-flex align-center ga-1">
                    <v-progress-linear
                      :model-value="mem.importance * 100"
                      color="warning"
                      height="3"
                      rounded
                      style="width: 40px;"
                    />
                  </div>
                </div>
              </v-card-text>
            </v-card>
          </div>
          </TransitionGroup>
        </div>
      </div>
    </template>

    <div v-else class="text-center text-medium-emphasis pa-12">
      <v-icon icon="mdi-timeline-clock-outline" size="40" class="mb-2 d-block mx-auto" style="opacity: 0.2;" />
      No memories found
    </div>

    <!-- Load More -->
    <div v-if="hasMore && !loading" class="text-center mt-4 mb-6">
      <v-btn variant="tonal" color="primary" :loading="loadingMore" @click="loadMore">
        Load more
      </v-btn>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, watch, onMounted, onUnmounted } from 'vue'
import { useTimeline } from '@/composables/useTimeline'
import { useStatsStore } from '@/stores/stats'
import { useSSE } from '@/composables/useSSE'

const statsStore = useStatsStore()
const {
  memories,
  loading,
  loadingMore,
  hasMore,
  categoryFilter,
  machineFilter,
  fetchTimeline,
  loadMore,
} = useTimeline()

const categoryOptions = computed(() => {
  if (!statsStore.stats) return []
  return statsStore.stats.by_category.map(c => c.category)
})

const machineOptions = computed(() => {
  if (!statsStore.stats) return []
  return statsStore.stats.by_machine.map(m => m.source_machine)
})

const groupedMemories = computed(() => {
  const groups: Record<string, typeof memories.value> = {}
  for (const mem of memories.value) {
    const day = new Date(mem.created_at).toLocaleDateString(undefined, {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    })
    if (!groups[day]) groups[day] = []
    groups[day].push(mem)
  }
  return Object.entries(groups).map(([date, items]) => ({ date, items }))
})

useSSE((evt) => {
  if (['memory_created', 'memory_updated', 'memory_deleted'].includes(evt.type)) {
    fetchTimeline({ silent: true })
  }
})

watch([categoryFilter, machineFilter], () => {
  fetchTimeline()  // filter change = intentional reload, show skeleton
})

// Queue poll: silent background refresh — no skeleton flash
let pollTimer: ReturnType<typeof setInterval> | null = null
function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') fetchTimeline({ silent: true })
  }, 8000)
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

onMounted(async () => {
  await statsStore.fetchStats()
  fetchTimeline()
  startPoll()
})

onUnmounted(stopPoll)
</script>

<style scoped>
.timeline-date-pill {
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
  font-size: 0.82rem;
  padding: 4px 14px;
  border-radius: 8px;
  white-space: nowrap;
}
.timeline-line {
  flex-grow: 1;
  height: 1px;
  background: rgba(255, 255, 255, 0.06);
  margin-left: 12px;
}
.timeline-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(var(--v-theme-primary));
  flex-shrink: 0;
  margin-top: 6px;
  transition: background 320ms ease, box-shadow 320ms ease;
}
.timeline-dot--pending {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-warning), 0.7);
  animation: dot-breathe 2.6s ease-in-out infinite;
}
.timeline-dot--failed {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-error), 0.55);
}
.timeline-stem {
  width: 1px;
  flex-grow: 1;
  background: rgba(255, 255, 255, 0.06);
  min-height: 8px;
  position: relative;
  overflow: hidden;
}
.timeline-stem--flow {
  background: linear-gradient(
    to bottom,
    rgba(var(--v-theme-warning), 0) 0%,
    rgba(var(--v-theme-warning), 0.35) 50%,
    rgba(var(--v-theme-warning), 0) 100%
  );
  background-size: 100% 220%;
  animation: stem-flow 2.8s linear infinite;
}
.timeline-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
  transition: border-color 150ms ease, background-color 320ms ease, opacity 320ms ease;
}
.timeline-card:hover {
  border-color: rgba(255, 255, 255, 0.1);
}

/* Ghost: memory in amber, not yet crystallized */
.timeline-card--ghost {
  background-color: color-mix(in oklch, rgb(var(--v-theme-surface)) 94%, rgb(var(--v-theme-warning)));
  border: 1px dashed rgba(var(--v-theme-warning), 0.28);
  position: relative;
}
.timeline-card--ghost::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  border-radius: inherit;
  background: linear-gradient(
    110deg,
    transparent 0%,
    transparent 40%,
    rgba(var(--v-theme-warning), 0.06) 50%,
    transparent 60%,
    transparent 100%
  );
  background-size: 280% 100%;
  animation: ghost-sweep 4.5s linear infinite;
}
.timeline-entry--ghost .ghost-text {
  font-style: italic;
  color: color-mix(in oklch, rgb(var(--v-theme-on-surface)) 78%, transparent);
}

/* Rejected: refined red restraint, not alarm */
.timeline-card--rejected {
  background-color: color-mix(in oklch, rgb(var(--v-theme-surface)) 96%, rgb(var(--v-theme-error)));
  border: 1px solid rgba(var(--v-theme-error), 0.18);
  opacity: 0.82;
}
.timeline-entry--rejected .ghost-text {
  color: color-mix(in oklch, rgb(var(--v-theme-on-surface)) 70%, transparent);
  text-decoration: line-through;
  text-decoration-color: rgba(var(--v-theme-error), 0.35);
  text-decoration-thickness: 1px;
}

.ghost-tilde {
  display: inline-block;
  width: 0.7em;
  margin-right: 2px;
  opacity: 0.85;
}

/* Typographic queue marker — no chip, just a quiet signal */
.queue-marker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.7rem;
  font-weight: 500;
  letter-spacing: 0.04em;
  text-transform: lowercase;
  font-variant-numeric: tabular-nums;
  padding: 0 4px;
  user-select: none;
}
.queue-marker--pending { color: rgb(var(--v-theme-warning)); }
.queue-marker--failed  { color: rgb(var(--v-theme-error)); }

.queue-marker__dots {
  display: inline-flex;
  gap: 2px;
  align-items: center;
}
.queue-marker__dots i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.35;
  animation: dot-wave 1.4s ease-in-out infinite;
}
.queue-marker__dots i:nth-child(2) { animation-delay: 0.18s; }
.queue-marker__dots i:nth-child(3) { animation-delay: 0.36s; }

/* Crystallization transition: ghost → stored */
.crystallize-enter-from {
  opacity: 0;
  transform: translateY(4px);
}
.crystallize-enter-active,
.crystallize-leave-active {
  transition: opacity 340ms ease, transform 340ms ease;
}
.crystallize-leave-to {
  opacity: 0;
  transform: translateY(-2px);
}
.crystallize-move { transition: transform 420ms cubic-bezier(0.22, 0.61, 0.36, 1); }

@keyframes dot-breathe {
  0%, 100% { box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-warning), 0.35); }
  50%      { box-shadow: inset 0 0 0 1.5px rgba(var(--v-theme-warning), 0.85); }
}
@keyframes stem-flow {
  0%   { background-position: 0 100%; }
  100% { background-position: 0 -100%; }
}
@keyframes ghost-sweep {
  0%   { background-position: -50% 0; }
  100% { background-position: 150% 0; }
}
@keyframes dot-wave {
  0%, 100% { opacity: 0.25; transform: translateY(0); }
  50%      { opacity: 1;    transform: translateY(-1.5px); }
}
@media (prefers-reduced-motion: reduce) {
  .timeline-dot--pending,
  .timeline-stem--flow,
  .timeline-card--ghost::before,
  .queue-marker__dots i {
    animation: none;
  }
}
.skeleton-block {
  background: linear-gradient(90deg, rgb(var(--v-theme-surface)) 25%, rgba(255,255,255,0.03) 50%, rgb(var(--v-theme-surface)) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 12px;
  height: 120px;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
</style>
