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
          <div
            v-for="mem in group.items"
            :key="mem.id"
            class="timeline-entry d-flex ga-3 mb-3"
          >
            <!-- Time -->
            <div class="text-caption text-medium-emphasis pt-1" style="min-width: 56px; font-variant-numeric: tabular-nums;">
              {{ new Date(mem.created_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) }}
            </div>

            <!-- Dot + line -->
            <div class="d-flex flex-column align-center" style="width: 12px;">
              <div class="timeline-dot" />
              <div class="timeline-stem" />
            </div>

            <!-- Card -->
            <v-card variant="flat" class="flex-grow-1 timeline-card">
              <v-card-text class="pa-3">
                <div class="text-body-2 font-weight-medium mb-1" style="line-height: 1.5;">
                  {{ mem.summary || mem.content.slice(0, 200) + (mem.content.length > 200 ? '...' : '') }}
                </div>
                <div v-if="mem.summary" class="text-body-2 text-medium-emphasis mb-2" style="line-height: 1.5;">
                  {{ mem.content.slice(0, 180) }}{{ mem.content.length > 180 ? '...' : '' }}
                </div>
                <div class="d-flex ga-2 align-center flex-wrap">
                  <v-chip v-if="mem.category" size="x-small" variant="tonal" color="primary" class="font-weight-medium">
                    {{ mem.category }}
                  </v-chip>
                  <v-chip v-if="mem.source_machine" size="x-small" variant="tonal" color="secondary" class="font-weight-medium">
                    {{ mem.source_machine }}
                  </v-chip>
                  <v-spacer />
                  <div v-if="mem.importance > 0" class="d-flex align-center ga-1">
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
import { computed, watch, onMounted } from 'vue'
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
    fetchTimeline()
  }
})

watch([categoryFilter, machineFilter], () => {
  fetchTimeline()
})

onMounted(async () => {
  await statsStore.fetchStats()
  fetchTimeline()
})
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
}
.timeline-stem {
  width: 1px;
  flex-grow: 1;
  background: rgba(255, 255, 255, 0.06);
  min-height: 8px;
}
.timeline-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
  transition: border-color 150ms ease;
}
.timeline-card:hover {
  border-color: rgba(255, 255, 255, 0.1);
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
