<template>
  <v-app-bar color="#161b22" density="comfortable" elevation="2">
    <v-toolbar-title class="font-weight-bold text-primary ml-4">nobrainr</v-toolbar-title>

    <v-tabs v-model="activeTab" color="primary" class="ml-6">
      <v-tab v-for="link in navLinks" :key="link.to" :to="link.to" :value="link.to">
        <v-icon :icon="link.icon" class="mr-2" size="small" />
        {{ link.label }}
      </v-tab>
    </v-tabs>

    <v-spacer />

    <div class="d-flex ga-2 mr-4" v-if="statsStore.stats">
      <v-chip size="small" variant="tonal" color="primary">
        <v-icon icon="mdi-brain" size="x-small" class="mr-1" />
        {{ statsStore.stats.total_memories }}
      </v-chip>
      <v-chip size="small" variant="tonal" color="secondary">
        <v-icon icon="mdi-shape-outline" size="x-small" class="mr-1" />
        {{ statsStore.stats.total_entities }}
      </v-chip>
      <v-chip size="small" variant="tonal" color="success">
        <v-icon icon="mdi-link-variant" size="x-small" class="mr-1" />
        {{ statsStore.stats.total_relations }}
      </v-chip>
    </div>
  </v-app-bar>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { useStatsStore } from '@/stores/stats'

const route = useRoute()
const statsStore = useStatsStore()
const activeTab = ref(route.path)

const navLinks = [
  { to: '/graph', label: 'Graph', icon: 'mdi-graph-outline' },
  { to: '/memories', label: 'Memories', icon: 'mdi-brain' },
  { to: '/timeline', label: 'Timeline', icon: 'mdi-timeline-clock-outline' },
  { to: '/scheduler', label: 'Scheduler', icon: 'mdi-calendar-clock' },
]
</script>
