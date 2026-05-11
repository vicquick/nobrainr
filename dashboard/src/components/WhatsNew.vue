<template>
  <RouterLink
    v-if="insight"
    :to="{ path: '/insights' }"
    class="cp-whatsnew"
    :title="`Open Insights (${formatRelative(insight.created_at)})`"
  >
    <p class="cp-whatsnew-eyebrow">
      <span class="cp-whatsnew-glyph" aria-hidden="true">❦</span>
      Today
      <span class="cp-whatsnew-meta">· {{ formatRelative(insight.created_at) }}</span>
    </p>
    <p class="cp-whatsnew-text">{{ trim(insight.summary || insight.content) }}</p>
    <p v-if="statsStore.stats" class="cp-whatsnew-stats">
      <em>{{ statsStore.stats.total_memories.toLocaleString() }}</em> entries
      <span class="cp-whatsnew-sep">·</span>
      <em>{{ statsStore.stats.total_entities.toLocaleString() }}</em> entities
      <span class="cp-whatsnew-sep">·</span>
      <em>{{ statsStore.stats.total_relations.toLocaleString() }}</em> relations
    </p>
  </RouterLink>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import api from '@/api/client'
import { useStatsStore } from '@/stores/stats'

interface TodayInsight {
  id: string
  content?: string
  summary?: string
  created_at: string
}

const insight = ref<TodayInsight | null>(null)
const statsStore = useStatsStore()

function trim(s?: string): string {
  if (!s) return ''
  const t = s.replace(/^Synthesis:\s*/i, '').trim()
  return t.length <= 200 ? t : t.slice(0, 200).trimEnd() + '…'
}

function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

onMounted(async () => {
  // Fire stats fetch (idempotent — store guards itself) so the trio
  // counts show even on first visit. /api/insights/today is its own
  // endpoint with strong recency bias + daily-stable shuffle, so
  // hitting it from this component doesn't conflict with InsightsView.
  if (!statsStore.stats) statsStore.fetchStats()
  try {
    const { data } = await api.get<TodayInsight | null>('/api/insights/today')
    if (data && data.id) insight.value = data
  } catch {
    // Fail-soft — the widget renders nothing if the endpoint is down.
  }
})
</script>

<style scoped>
.cp-whatsnew {
  display: block;
  padding: 12px 14px;
  margin: 0 12px 8px;
  background:
    linear-gradient(180deg, rgba(200, 169, 110, 0.06), rgba(200, 169, 110, 0.02));
  border: 1px solid var(--cp-rule);
  border-left: 2px solid var(--cp-gold);
  border-radius: 2px;
  text-decoration: none;
  color: inherit;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    border-color var(--cp-dur-hover) var(--cp-ease),
    transform var(--cp-dur-hover) var(--cp-ease);
}
.cp-whatsnew:hover,
.cp-whatsnew:focus-visible {
  background:
    linear-gradient(180deg, rgba(200, 169, 110, 0.10), rgba(200, 169, 110, 0.04));
  border-left-color: var(--cp-gold-bright);
  transform: translateY(-1px);
}
.cp-whatsnew:focus-visible { outline: none; }

.cp-whatsnew-eyebrow {
  margin: 0 0 6px;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold);
  display: flex;
  align-items: center;
  gap: 8px;
}
.cp-whatsnew-glyph { color: var(--cp-gold); font-style: normal; }
.cp-whatsnew-meta {
  text-transform: none;
  letter-spacing: 0.04em;
  color: var(--cp-ink-faint);
}
.cp-whatsnew-text {
  margin: 0 0 8px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--cp-ink);
}
.cp-whatsnew-stats {
  margin: 0;
  font-style: italic;
  font-size: 10.5px;
  color: var(--cp-ink-faint);
  letter-spacing: 0.04em;
}
.cp-whatsnew-stats em {
  font-style: normal;
  font-variant-numeric: tabular-nums;
  color: var(--cp-ink-mute);
}
.cp-whatsnew-sep { color: var(--cp-gold-soft); margin: 0 4px; }
</style>
