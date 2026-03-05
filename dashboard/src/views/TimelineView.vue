<template>
  <v-container fluid>
    <!-- Filter Bar -->
    <div class="d-flex ga-3 mb-4 align-center">
      <v-select
        v-model="categoryFilter"
        :items="categoryOptions"
        label="Category"
        variant="outlined"
        density="compact"
        clearable
        hide-details
        style="max-width: 200px;"
      />
      <v-select
        v-model="machineFilter"
        :items="machineOptions"
        label="Machine"
        variant="outlined"
        density="compact"
        clearable
        hide-details
        style="max-width: 200px;"
      />
    </div>

    <!-- Loading -->
    <template v-if="loading">
      <v-skeleton-loader type="article" class="mb-4" />
      <v-skeleton-loader type="article" class="mb-4" />
    </template>

    <!-- Timeline -->
    <template v-else-if="groupedMemories.length">
      <div v-for="group in groupedMemories" :key="group.date" class="mb-6">
        <div class="text-subtitle-1 font-weight-bold text-primary mb-3">{{ group.date }}</div>
        <v-timeline side="end" density="compact" line-color="rgba(255,255,255,0.1)">
          <v-timeline-item
            v-for="mem in group.items"
            :key="mem.id"
            dot-color="primary"
            size="x-small"
          >
            <v-card variant="outlined" class="mb-1">
              <v-card-text class="pa-3">
                <div class="text-body-2 font-weight-medium mb-1">
                  {{ mem.summary || mem.content.slice(0, 200) + (mem.content.length > 200 ? '...' : '') }}
                </div>
                <div v-if="mem.summary" class="text-body-2 text-medium-emphasis mb-2">
                  {{ mem.content.slice(0, 200) }}{{ mem.content.length > 200 ? '...' : '' }}
                </div>
                <div class="d-flex ga-2 align-center flex-wrap">
                  <v-chip v-if="mem.category" size="x-small" variant="tonal" color="primary">
                    {{ mem.category }}
                  </v-chip>
                  <v-chip v-if="mem.source_machine" size="x-small" variant="tonal" color="secondary">
                    {{ mem.source_machine }}
                  </v-chip>
                  <v-spacer />
                  <v-progress-linear
                    v-if="mem.importance > 0"
                    :model-value="mem.importance * 100"
                    color="warning"
                    height="4"
                    rounded
                    style="max-width: 60px;"
                  />
                  <span class="text-caption text-medium-emphasis">
                    {{ new Date(mem.created_at).toLocaleTimeString() }}
                  </span>
                </div>
              </v-card-text>
            </v-card>
          </v-timeline-item>
        </v-timeline>
      </div>
    </template>

    <div v-else class="text-center text-medium-emphasis pa-8">
      No memories found
    </div>

    <!-- Load More -->
    <div v-if="hasMore && !loading" class="text-center mt-4 mb-4">
      <v-btn variant="outlined" color="primary" :loading="loadingMore" @click="loadMore">
        Load more
      </v-btn>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, watch, onMounted } from 'vue'
import { useTimeline } from '@/composables/useTimeline'
import { useStatsStore } from '@/stores/stats'

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
  return Object.keys(statsStore.stats.by_category)
})

const machineOptions = computed(() => {
  if (!statsStore.stats) return []
  return Object.keys(statsStore.stats.by_machine)
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

watch([categoryFilter, machineFilter], () => {
  fetchTimeline()
})

onMounted(async () => {
  await statsStore.fetchStats()
  fetchTimeline()
})
</script>
