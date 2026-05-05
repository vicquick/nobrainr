<template>
  <v-app-bar color="transparent" density="comfortable" elevation="0" class="app-bar-folio">
    <template #prepend>
      <div class="d-flex align-center ml-3 brand-mark">
        <span class="brand-ornament">❦</span>
        <span class="brand-name">
          <span class="brand-no">no</span><span class="brand-brainr">brainr</span>
        </span>
      </div>
    </template>

    <!-- Desktop/Tablet: inline nav links -->
    <div v-if="!mobile" class="d-flex align-center ml-1 ml-sm-6 nav-links">
      <RouterLink
        v-for="link in navLinks"
        :key="link.to"
        :to="link.to"
        class="folio-link"
        :class="{ active: route.path === link.to }"
      >
        <v-icon v-if="smAndDown" :icon="link.icon" size="14" />
        <span v-else>{{ link.label }}</span>
      </RouterLink>
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

    <div class="d-flex align-center ga-3 mr-3 folio-stats" v-if="statsStore.stats && !smAndDown">
      <span class="stat-line">
        <span class="stat-num">{{ statsStore.stats.total_memories.toLocaleString() }}</span>
        <span class="stat-label">mem.</span>
      </span>
      <span class="stat-divider">·</span>
      <span class="stat-line">
        <span class="stat-num">{{ statsStore.stats.total_entities.toLocaleString() }}</span>
        <span class="stat-label">ent.</span>
      </span>
      <span class="stat-divider">·</span>
      <span class="stat-line">
        <span class="stat-num">{{ statsStore.stats.total_relations.toLocaleString() }}</span>
        <span class="stat-label">rel.</span>
      </span>
    </div>

    <button
      class="chat-toggle"
      :class="{ active: chatStore.isOpen }"
      @click="chatStore.toggle()"
      aria-label="Toggle chat"
    >
      <v-icon icon="mdi-chat-outline" size="16" />
    </button>
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
import { useRoute, RouterLink } from 'vue-router'
import { useDisplay } from 'vuetify'
import { useStatsStore } from '@/stores/stats'
import { useChatStore } from '@/stores/chat'

const route = useRoute()
const { mobile, smAndDown } = useDisplay()
const statsStore = useStatsStore()
const chatStore = useChatStore()
const drawer = ref(false)

const navLinks = [
  { to: '/commonplace', label: 'Commonplace', icon: 'mdi-book-open-page-variant' },
  { to: '/insights', label: 'Insights', icon: 'mdi-lightbulb-on-outline' },
  { to: '/memories', label: 'Memories', icon: 'mdi-brain' },
  { to: '/galaxy', label: 'Galaxy', icon: 'mdi-creation' },
  { to: '/graph', label: 'Graph', icon: 'mdi-graph-outline' },
  { to: '/timeline', label: 'Timeline', icon: 'mdi-timeline-clock-outline' },
  { to: '/scheduler', label: 'Scheduler', icon: 'mdi-calendar-clock' },
  { to: '/pulse', label: 'Pulse', icon: 'mdi-pulse' },
]
</script>

<style scoped>
.app-bar-folio {
  background: linear-gradient(180deg, rgba(14, 11, 6, 0.92), rgba(18, 14, 8, 0.88)) !important;
  border-bottom: 1px solid rgba(200, 169, 110, 0.18) !important;
  backdrop-filter: blur(8px);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}

/* Brand */
.brand-mark { gap: 10px; }
.brand-ornament {
  color: #c8a96e;
  font-size: 18px;
  line-height: 1;
}
.brand-name {
  font-family: Georgia, serif;
  font-size: 18px;
  letter-spacing: 0.06em;
  font-style: italic;
}
.brand-no { color: #c8a96e; }
.brand-brainr { color: rgba(238, 224, 196, 0.94); }

/* Nav links */
.nav-links { gap: 0; }
.folio-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 14px;
  font-family: Georgia, serif;
  font-size: 13px;
  letter-spacing: 0.08em;
  color: rgba(238, 224, 196, 0.55);
  text-decoration: none;
  font-style: italic;
  border-bottom: 1px solid transparent;
  transition: all 180ms cubic-bezier(0.22, 1, 0.36, 1);
  position: relative;
}
.folio-link:hover {
  color: rgba(238, 224, 196, 0.92);
}
.folio-link.active {
  color: #c8a96e;
  font-style: normal;
}
.folio-link.active::after {
  content: '';
  position: absolute;
  bottom: -2px;
  left: 12px;
  right: 12px;
  height: 1px;
  background: linear-gradient(
    90deg, transparent, #c8a96e 30%, #c8a96e 70%, transparent
  );
}

/* Stats */
.folio-stats {
  font-family: Georgia, serif;
  font-size: 12px;
  color: rgba(238, 224, 196, 0.65);
}
.stat-line {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-variant-numeric: tabular-nums;
}
.stat-num {
  color: #c8a96e;
  font-weight: 400;
}
.stat-label {
  font-style: italic;
  color: rgba(238, 224, 196, 0.45);
  font-size: 11px;
  letter-spacing: 0.05em;
}
.stat-divider {
  color: rgba(200, 169, 110, 0.4);
}

/* Chat toggle */
.chat-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  background: transparent;
  border: 1px solid rgba(200, 169, 110, 0.25);
  color: rgba(238, 224, 196, 0.65);
  cursor: pointer;
  transition: all 150ms;
}
.chat-toggle:hover {
  border-color: #c8a96e;
  color: #c8a96e;
}
.chat-toggle.active {
  background: rgba(200, 169, 110, 0.15);
  border-color: #c8a96e;
  color: #c8a96e;
}

/* Mobile drawer */
.mobile-nav {
  background: linear-gradient(180deg, #14110a, #0e0b06) !important;
  border-right: 1px solid rgba(200, 169, 110, 0.18) !important;
  font-family: Georgia, serif;
}
:deep(.mobile-nav .v-list-item) {
  font-family: Georgia, serif;
  font-style: italic;
  letter-spacing: 0.05em;
  color: rgba(238, 224, 196, 0.7);
}
:deep(.mobile-nav .v-list-item--active) {
  color: #c8a96e !important;
}
</style>
