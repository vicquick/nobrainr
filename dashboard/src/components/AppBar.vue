<template>
  <v-app-bar color="transparent" density="comfortable" elevation="0" class="app-bar-folio">
    <!-- LEFT: side-panel toggle. Custom SVG (Lucide-style panel-left).
         Stroke-only, rounded rect with one vertical line — the icon
         Claude / MetaMCP / most modern apps use. -->
    <template #prepend>
      <button
        class="panel-toggle"
        :class="{ active: drawer }"
        @click="drawer = !drawer"
        aria-label="Toggle navigation"
      >
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none"
             stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="3" width="20" height="18" rx="2" stroke-width="2" />
          <path d="M9 3V21" stroke-width="2" />
        </svg>
      </button>
    </template>

    <!-- CENTER: brand mark + (desktop only via CSS) inline nav. -->
    <div class="bar-center">
      <RouterLink to="/commonplace" class="brand-mark" aria-label="Home">
        <span class="brand-ornament">❦</span>
        <span class="brand-name">
          <span class="brand-no">no</span><span class="brand-brainr">brainr</span>
        </span>
      </RouterLink>

      <nav class="nav-links">
        <RouterLink
          v-for="link in navLinks"
          :key="link.to"
          :to="link.to"
          class="folio-link"
          :class="{ active: route.path === link.to }"
        >
          {{ link.label }}
        </RouterLink>
      </nav>
    </div>

    <!-- RIGHT: stats (desktop-only via CSS) + chat toggle -->
    <template #append>
      <div class="bar-end">
        <div class="folio-stats" v-if="statsStore.stats">
          <span class="stat-line">
            <span class="stat-num">{{ memoriesTicker.toLocaleString() }}</span>
            <span class="stat-label">mem.</span>
          </span>
          <span class="stat-divider">·</span>
          <span class="stat-line">
            <span class="stat-num">{{ entitiesTicker.toLocaleString() }}</span>
            <span class="stat-label">ent.</span>
          </span>
          <span class="stat-divider">·</span>
          <span class="stat-line">
            <span class="stat-num">{{ relationsTicker.toLocaleString() }}</span>
            <span class="stat-label">rel.</span>
          </span>
        </div>

        <button
          class="chat-toggle"
          :class="{ active: chatStore.isOpen }"
          @click="chatStore.toggle()"
          aria-label="Toggle chat"
        >
          <svg viewBox="0 0 24 24" width="17" height="17" fill="none"
               stroke="currentColor" stroke-width="1.7"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
          </svg>
        </button>
      </div>
    </template>
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
    <v-divider class="my-2 drawer-rule" />
    <!-- Drawer stats: codex-styled to match the desktop bar voice
         (italic small caps, gold ink) instead of Vuetify's chip chrome. -->
    <div class="px-4 py-2 drawer-stats" v-if="statsStore.stats">
      <p class="drawer-stats-eyebrow">Codex · vital signs</p>
      <ul class="drawer-stats-list">
        <li class="drawer-stat">
          <span class="drawer-stat-num">{{ memoriesTicker.toLocaleString() }}</span>
          <span class="drawer-stat-label">memories scribed</span>
        </li>
        <li class="drawer-stat">
          <span class="drawer-stat-num">{{ entitiesTicker.toLocaleString() }}</span>
          <span class="drawer-stat-label">entities catalogued</span>
        </li>
        <li class="drawer-stat">
          <span class="drawer-stat-num">{{ relationsTicker.toLocaleString() }}</span>
          <span class="drawer-stat-label">relations drawn</span>
        </li>
      </ul>
    </div>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { useDisplay } from 'vuetify'
import { useStatsStore } from '@/stores/stats'
import { useChatStore } from '@/stores/chat'
import { useCountUp } from '@/composables/useCountUp'

const route = useRoute()
const { mobile, smAndDown } = useDisplay()
const statsStore = useStatsStore()
const chatStore = useChatStore()
const drawer = ref(false)

// Count-up tickers on first paint so the AppBar's vital-signs trio
// behaves the same as Scheduler's Vitae quire — readers' eyes land
// on a moving number, not a static one. useCountUp animates only the
// first non-zero arrival; subsequent polls snap.
const memoriesTicker  = useCountUp(computed(() => statsStore.stats?.total_memories  ?? 0))
const entitiesTicker  = useCountUp(computed(() => statsStore.stats?.total_entities  ?? 0))
const relationsTicker = useCountUp(computed(() => statsStore.stats?.total_relations ?? 0))

const navLinks = [
  { to: '/commonplace', label: 'Commonplace', icon: 'mdi-book-open-page-variant' },
  { to: '/library', label: 'Library', icon: 'mdi-bookshelf' },
  { to: '/insights', label: 'Insights', icon: 'mdi-lightbulb-on-outline' },
  { to: '/memories', label: 'Memories', icon: 'mdi-brain' },
  { to: '/galaxy', label: 'Galaxy', icon: 'mdi-creation' },
  { to: '/graph', label: 'Graph', icon: 'mdi-graph-outline' },
  { to: '/constellarium', label: 'Constellarium', icon: 'mdi-star-four-points-outline' },
  { to: '/timeline', label: 'Timeline', icon: 'mdi-timeline-clock-outline' },
  { to: '/scheduler', label: 'Scheduler', icon: 'mdi-calendar-clock' },
  { to: '/pulse', label: 'Pulse', icon: 'mdi-pulse' },
  { to: '/scriptorium', label: 'Scriptorium', icon: 'mdi-feather' },
]
</script>

<style scoped>
/* Lock the bar to viewport width and prevent any post-hydration shift.
   Pure CSS media queries handle the breakpoint hiding so there's no
   "desktop nav flashes then disappears" flicker. */
.app-bar-folio {
  background: linear-gradient(180deg, rgba(14, 11, 6, 0.92), rgba(18, 14, 8, 0.88)) !important;
  border-bottom: 1px solid rgba(200, 169, 110, 0.18) !important;
  backdrop-filter: blur(8px);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  width: 100% !important;
  max-width: 100vw !important;
  left: 0 !important;
  right: 0 !important;
}

/* Vuetify slots — strip default padding so center expands fully and
   the bar doesn't widen past the viewport. */
.app-bar-folio :deep(.v-toolbar__content) {
  padding: 0 6px !important;
  max-width: 100vw;
  overflow: hidden;
}
.app-bar-folio :deep(.v-toolbar__prepend),
.app-bar-folio :deep(.v-toolbar__append) {
  margin: 0 !important;
  padding: 0 !important;
}

/* Side-panel + chat toggles share size for visual balance */
.panel-toggle, .chat-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  margin: 0 4px;
  background: transparent;
  border: 1px solid rgba(200, 169, 110, 0.25);
  border-radius: 8px;
  color: rgba(238, 224, 196, 0.78);
  cursor: pointer;
  transition: all 150ms;
  flex-shrink: 0;
  padding: 0;
}
.panel-toggle:hover, .chat-toggle:hover {
  border-color: #c8a96e;
  color: #c8a96e;
}
.panel-toggle.active, .chat-toggle.active {
  background: rgba(200, 169, 110, 0.12);
  border-color: #c8a96e;
  color: #c8a96e;
}
.panel-toggle svg, .chat-toggle svg {
  display: block;
}

/* CENTER block — brand + (desktop) nav. Use min-width:0 so flexbox can
   shrink it below content size and prevent overflow on narrow viewports. */
.bar-center {
  flex: 1 1 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 28px;
  min-width: 0;
  padding: 0 6px;
  overflow: hidden;
}

.brand-mark {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
  min-width: 0;
}
.brand-ornament {
  color: #c8a96e;
  font-size: 16px;
  line-height: 1;
  align-self: center;
  flex-shrink: 0;
}
.brand-name {
  font-family: Georgia, serif;
  font-size: 17px;
  letter-spacing: 0.05em;
  font-style: italic;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.brand-no { color: #c8a96e; }
.brand-brainr { color: rgba(238, 224, 196, 0.94); }

/* Nav links — visible at ≥768px only (desktop+tablet wide). Below that,
   the drawer (panel-toggle) handles navigation. */
.nav-links {
  display: none;
  align-items: center;
  gap: 0;
  flex-shrink: 1;
  min-width: 0;
}
@media (min-width: 768px) {
  .nav-links { display: flex; }
}

.folio-link {
  display: inline-flex;
  align-items: center;
  padding: 4px 11px;
  font-family: Georgia, serif;
  font-size: 13px;
  letter-spacing: 0.06em;
  color: rgba(238, 224, 196, 0.55);
  text-decoration: none;
  font-style: italic;
  transition:
    color var(--cp-dur-hover) var(--cp-ease),
    font-style var(--cp-dur-hover) var(--cp-ease);
  position: relative;
  white-space: nowrap;
}
.folio-link:hover { color: rgba(238, 224, 196, 0.92); }
.folio-link.active {
  color: var(--cp-gold);
  font-style: normal;
}
/* Gold underline on every link, but invisible until .active. The
   underline already exists on inactive links — toggling opacity (a
   composite property) animates between routes instead of the
   pseudo-element popping in. */
.folio-link::after {
  content: '';
  position: absolute;
  bottom: -2px;
  left: 10px;
  right: 10px;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--cp-gold) 30%, var(--cp-gold) 70%, transparent);
  opacity: 0;
  transform: scaleX(0.6);
  transform-origin: 50% 50%;
  transition:
    opacity var(--cp-dur-panel) var(--cp-ease-decel),
    transform var(--cp-dur-panel) var(--cp-ease-decel);
}
.folio-link.active::after {
  opacity: 1;
  transform: scaleX(1);
}

/* RIGHT: stats (≥1100px only — keeps the bar compact otherwise) + chat */
.bar-end {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-right: 4px;
  flex-shrink: 0;
}
.folio-stats {
  display: none;
  font-family: Georgia, serif;
  font-size: 12px;
  color: rgba(238, 224, 196, 0.65);
  align-items: baseline;
  gap: 8px;
}
@media (min-width: 1100px) {
  .folio-stats { display: flex; }
}
.stat-line {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-variant-numeric: tabular-nums;
}
.stat-num { color: #c8a96e; font-weight: 400; }
.stat-label {
  font-style: italic;
  color: rgba(238, 224, 196, 0.45);
  font-size: 11px;
  letter-spacing: 0.05em;
}
.stat-divider { color: rgba(200, 169, 110, 0.4); }

/* Mobile (<480px): tighten everything, drop the ornament */
@media (max-width: 480px) {
  .brand-ornament { display: none; }
  .brand-name { font-size: 16px; }
  .panel-toggle, .chat-toggle { width: 32px; height: 32px; }
  .bar-center { gap: 0; padding: 0 4px; }
  .panel-toggle, .chat-toggle { margin: 0 2px; }
}

/* Mobile drawer */
.mobile-nav {
  background: linear-gradient(180deg, var(--cp-paper), var(--cp-paper-deep)) !important;
  border-right: 1px solid var(--cp-rule) !important;
  font-family: Georgia, serif;
}
:deep(.mobile-nav .v-list-item) {
  font-family: Georgia, serif;
  font-style: italic;
  letter-spacing: 0.05em;
  color: var(--cp-ink-mute);
}
:deep(.mobile-nav .v-list-item--active) { color: var(--cp-gold) !important; }
.drawer-rule :deep(hr),
.drawer-rule {
  border-color: var(--cp-gold-faint) !important;
  opacity: 0.6;
}

/* Codex-styled drawer stats — replaces the previous Vuetify v-chip
   stack with hand-drawn typographic vital signs that match the
   desktop bar's tabular-num gold-ink language. */
.drawer-stats { font-family: Georgia, serif; }
.drawer-stats-eyebrow {
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  margin: 0 0 8px;
}
.drawer-stats-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.drawer-stat {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  gap: 8px;
}
.drawer-stat-num {
  font-size: 16px;
  font-variant-numeric: tabular-nums;
  color: var(--cp-gold);
}
.drawer-stat-label {
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--cp-ink-mute);
}
</style>
