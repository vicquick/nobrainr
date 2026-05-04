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

    <!-- Desktop/Tablet: inline nav links -->
    <div v-if="!mobile" class="d-flex align-center ml-1 ml-sm-6 nav-links">
      <v-btn
        v-for="link in navLinks"
        :key="link.to"
        :to="link.to"
        :prepend-icon="smAndDown ? undefined : link.icon"
        :icon="smAndDown ? link.icon : undefined"
        :variant="route.path === link.to ? 'tonal' : 'text'"
        :color="route.path === link.to ? 'primary' : undefined"
        rounded="lg"
        :size="smAndDown ? 'x-small' : 'small'"
        class="mx-0 mx-sm-1 text-none nav-btn"
        :class="{ 'active-nav': route.path === link.to }"
        style="letter-spacing: 0;"
      >
        <template v-if="!smAndDown">{{ link.label }}</template>
      </v-btn>
    </div>

    <!-- Mobile: hamburger menu -->
    <v-btn
      v-if="mobile"
      icon="mdi-menu"
      variant="text"
      size="small"
      class="ml-2"
      @click="drawer = !drawer"
    />

    <v-spacer />

    <div class="d-flex align-center ga-2 mr-2" v-if="statsStore.stats && !smAndDown">
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

    <v-btn
      icon="mdi-chat-outline"
      variant="text"
      size="small"
      class="mr-2"
      :color="chatStore.isOpen ? 'primary' : undefined"
      @click="chatStore.toggle()"
    />
  </v-app-bar>

  <!-- Mobile navigation drawer -->
  <v-navigation-drawer
    v-model="drawer"
    location="left"
    temporary
    width="240"
    class="mobile-nav"
  >
    <v-list nav density="compact" class="mt-2">
      <v-list-item
        v-for="link in navLinks"
        :key="link.to"
        :to="link.to"
        :prepend-icon="link.icon"
        :title="link.label"
        :active="route.path === link.to"
        color="primary"
        rounded="lg"
        @click="drawer = false"
      />
    </v-list>
    <v-divider class="my-2" />
    <div class="px-4 py-2" v-if="statsStore.stats">
      <div class="text-caption text-medium-emphasis mb-2">Knowledge Base</div>
      <div class="d-flex flex-column ga-1">
        <v-chip size="small" variant="tonal" color="primary" class="stat-chip">
          <v-icon icon="mdi-brain" size="12" class="mr-1" />
          {{ statsStore.stats.total_memories.toLocaleString() }} memories
        </v-chip>
        <v-chip size="small" variant="tonal" color="secondary" class="stat-chip">
          <v-icon icon="mdi-shape-outline" size="12" class="mr-1" />
          {{ statsStore.stats.total_entities.toLocaleString() }} entities
        </v-chip>
        <v-chip size="small" variant="tonal" color="success" class="stat-chip">
          <v-icon icon="mdi-link-variant" size="12" class="mr-1" />
          {{ statsStore.stats.total_relations.toLocaleString() }} relations
        </v-chip>
      </div>
    </div>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { useDisplay } from 'vuetify'
import { useStatsStore } from '@/stores/stats'
import { useChatStore } from '@/stores/chat'

const route = useRoute()
const { mobile, smAndDown } = useDisplay()
const statsStore = useStatsStore()
const chatStore = useChatStore()
const drawer = ref(false)

const navLinks = [
  { to: '/galaxy', label: 'Galaxy', icon: 'mdi-creation' },
  { to: '/graph', label: 'Graph', icon: 'mdi-graph-outline' },
  { to: '/memories', label: 'Memories', icon: 'mdi-brain' },
  { to: '/timeline', label: 'Timeline', icon: 'mdi-timeline-clock-outline' },
  { to: '/scheduler', label: 'Scheduler', icon: 'mdi-calendar-clock' },
  { to: '/pulse', label: 'Pulse', icon: 'mdi-pulse' },
  { to: '/commonplace', label: 'Commonplace', icon: 'mdi-book-open-page-variant' },
]
</script>

<style scoped>
.app-bar-glass {
  background: rgb(var(--v-theme-surface));
  border-bottom: 1px solid rgba(255, 255, 255, 0.04) !important;
}
.stat-chip {
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}
.mobile-nav {
  background: rgb(var(--v-theme-surface)) !important;
}
</style>
