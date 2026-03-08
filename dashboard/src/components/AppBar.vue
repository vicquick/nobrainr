<template>
  <v-app-bar color="rgba(10, 14, 20, 0.85)" density="comfortable" elevation="0" class="app-bar-glass">
    <template #prepend>
      <div class="d-flex align-center ml-3">
        <v-icon icon="mdi-brain" color="primary" size="24" class="mr-2" />
        <span class="text-h6 font-weight-bold" style="letter-spacing: -0.5px;">
          <span class="text-primary">no</span><span class="text-high-emphasis">brainr</span>
        </span>
      </div>
    </template>

    <div class="d-flex align-center ml-6">
      <v-btn
        v-for="link in navLinks"
        :key="link.to"
        :to="link.to"
        :prepend-icon="link.icon"
        :variant="route.path === link.to ? 'tonal' : 'text'"
        :color="route.path === link.to ? 'primary' : undefined"
        rounded="lg"
        size="small"
        class="mx-1 text-none"
        style="letter-spacing: 0;"
      >
        {{ link.label }}
      </v-btn>
    </div>

    <v-spacer />

    <div class="d-flex align-center ga-2 mr-4" v-if="statsStore.stats">
      <v-chip size="small" variant="tonal" color="primary" class="stat-chip">
        <v-icon icon="mdi-brain" size="12" class="mr-1" />
        {{ statsStore.stats.total_memories.toLocaleString() }}
      </v-chip>
      <v-chip size="small" variant="tonal" color="secondary" class="stat-chip">
        <v-icon icon="mdi-shape-outline" size="12" class="mr-1" />
        {{ statsStore.stats.total_entities.toLocaleString() }}
      </v-chip>
      <v-chip size="small" variant="tonal" color="success" class="stat-chip">
        <v-icon icon="mdi-link-variant" size="12" class="mr-1" />
        {{ statsStore.stats.total_relations.toLocaleString() }}
      </v-chip>
    </div>
  </v-app-bar>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import { useStatsStore } from '@/stores/stats'

const route = useRoute()
const statsStore = useStatsStore()

const navLinks = [
  { to: '/graph', label: 'Graph', icon: 'mdi-graph-outline' },
  { to: '/memories', label: 'Memories', icon: 'mdi-brain' },
  { to: '/timeline', label: 'Timeline', icon: 'mdi-timeline-clock-outline' },
  { to: '/scheduler', label: 'Scheduler', icon: 'mdi-calendar-clock' },
]
</script>

<style scoped>
.app-bar-glass {
  backdrop-filter: blur(12px) saturate(180%);
  border-bottom: 1px solid rgba(255, 255, 255, 0.04) !important;
}
.stat-chip {
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}
</style>
