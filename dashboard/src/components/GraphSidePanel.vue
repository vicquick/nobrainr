<template>
  <div v-if="node" class="d-flex flex-column fill-height">
    <!-- Header -->
    <div class="panel-header pa-4">
      <div class="d-flex align-center mb-2">
        <EntityBadge :type="node.entity.entity_type" />
        <v-spacer />
        <v-btn icon="mdi-close" variant="text" size="x-small" aria-label="Close panel" @click="$emit('close')" />
      </div>
      <div class="entity-name">
        {{ node.entity.canonical_name }}
      </div>
      <div v-if="node.entity.description" class="entity-description mt-1">
        {{ node.entity.description }}
      </div>
      <div class="d-flex ga-3 mt-3">
        <div class="stat-item">
          <v-icon icon="mdi-eye-outline" size="13" />
          {{ node.entity.mention_count }} mentions
        </div>
        <div class="stat-item">
          <v-icon icon="mdi-clock-outline" size="13" />
          {{ new Date(node.entity.created_at).toLocaleDateString() }}
        </div>
      </div>
    </div>

    <!-- Content -->
    <div class="flex-grow-1 pa-4" style="overflow-y: auto;">
      <!-- Loading shimmer (visible only when navigating to another entity
           via connection click — keeps the panel from showing stale data
           paired with a spinner-less reflow). -->
      <div v-if="loading" class="panel-loading">
        <span class="folio-skel skel-line" />
        <span class="folio-skel skel-line short" />
        <span class="folio-skel skel-line" />
      </div>

      <template v-else>
        <!-- Connections -->
        <div v-if="node.connections.length" class="mb-5">
          <div class="section-header mb-3">
            <v-icon icon="mdi-link-variant" size="15" />
            <span>Connections</span>
            <span class="section-count">· {{ node.connections.length }}</span>
          </div>
          <div class="connections-list cp-stagger">
            <div
              v-for="(conn, i) in node.connections"
              :key="i"
              class="connection-item clickable"
              :style="staggerStyle(i)"
              tabindex="0"
              role="button"
              :aria-label="`Navigate to ${conn.connected_name} via ${conn.relationship_type}`"
              :title="`[click] explore ${conn.connected_name}`"
              @click="$emit('navigate', conn.connected_id)"
              @keydown.enter.prevent="$emit('navigate', conn.connected_id)"
              @keydown.space.prevent="$emit('navigate', conn.connected_id)"
            >
              <v-icon
                :icon="conn.direction === 'outgoing' ? 'mdi-arrow-right' : 'mdi-arrow-left'"
                size="13"
                :color="conn.direction === 'outgoing' ? '#7b8ec8' : '#6ba87a'"
                class="conn-arrow"
              />
              <div class="flex-grow-1" style="min-width: 0;">
                <span class="conn-relation">{{ conn.relationship_type }}</span>
                <span class="conn-target">{{ conn.connected_name }}</span>
              </div>
              <span class="conn-confidence">
                {{ (conn.confidence * 100).toFixed(0) }}%
              </span>
            </div>
          </div>
        </div>

        <!-- Related Memories -->
        <div v-if="node.memories.length">
          <div class="section-header mb-3">
            <v-icon icon="mdi-brain" size="15" />
            <span>Memories</span>
            <span class="section-count">· {{ node.memories.length }}</span>
          </div>
          <div class="d-flex flex-column ga-2 cp-stagger">
            <div
              v-for="(mem, i) in node.memories.slice(0, 15)"
              :key="mem.id"
              class="memory-item"
              :style="staggerStyle(i)"
            >
              <div class="memory-text mb-1">
                {{ mem.summary || trimContent(mem.content) }}
              </div>
              <div class="d-flex align-center ga-2">
                <span v-if="mem.category" class="memory-cat">{{ mem.category }}</span>
                <v-spacer />
                <span class="memory-date">
                  {{ new Date(mem.created_at).toLocaleDateString() }}
                </span>
              </div>
            </div>
          </div>
          <p v-if="node.memories.length > 15" class="memory-overflow">
            <em>+ {{ node.memories.length - 15 }} more in the gathering</em>
          </p>
        </div>

        <!-- Empty: no connections, no memories. The entity exists but has
             no marginalia or scribed entries — usually a freshly-extracted
             entity awaiting cross-references. -->
        <div
          v-if="!node.connections.length && !node.memories.length"
          class="panel-empty"
        >
          <p class="empty-headline">— this entry stands alone —</p>
          <p class="empty-hint">
            No connections drawn yet, no memories scribed against it.
            Likely freshly extracted — the relationship-linker scribe may
            yet weave it into the corpus.
          </p>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { NodeDetail } from '@/types'
import EntityBadge from './EntityBadge.vue'
import { staggerStyle } from '@/composables/useStaggerIndex'

defineProps<{
  node: NodeDetail | null
  loading?: boolean
}>()

defineEmits<{
  close: []
  navigate: [entityId: string]
}>()

function trimContent(content: string): string {
  if (!content) return ''
  if (content.length <= 120) return content
  return content.slice(0, 120).trimEnd() + '…'
}
</script>

<style scoped>
/* Graph side-panel rendered as folio marginalia — gold rules, italic
   serif throughout, no card chrome. */
.panel-header {
  border-bottom: 1px solid var(--cp-rule);
  background: var(--cp-gold-trace);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.entity-name {
  font-family: Georgia, serif;
  font-size: 22px;
  font-weight: 400;
  letter-spacing: 0.02em;
  color: rgba(238, 224, 196, 0.96);
  line-height: 1.3;
}
.entity-description {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  color: rgba(238, 224, 196, 0.72);
  line-height: 1.6;
}
.stat-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-mute);
  font-variant-numeric: tabular-nums;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold);
  font-weight: 400;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--cp-gold-faint);
}
.section-count {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.05em;
  text-transform: none;
  color: var(--cp-gold-soft);
  font-variant-numeric: tabular-nums;
  margin-left: 4px;
}

.connections-list {
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.connection-item {
  border-bottom: 1px dotted var(--cp-gold-faint);
  padding: 8px 6px;
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    transform var(--cp-dur-hover) var(--cp-ease);
  display: flex;
  align-items: baseline;
  font-family: Georgia, serif;
}
.connection-item:hover,
.connection-item:focus-visible {
  background: var(--cp-gold-trace);
  transform: translateX(2px);
}
.connection-item:focus-visible { outline: none; }
.clickable { cursor: pointer; }
.conn-arrow { margin-right: 8px; flex-shrink: 0; }
.conn-relation {
  display: inline-block;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
  margin-right: 8px;
}
.conn-target {
  font-family: Georgia, serif;
  font-size: 13px;
  color: rgba(238, 224, 196, 0.92);
}
.conn-confidence {
  font-family: Georgia, serif;
  font-variant-numeric: tabular-nums;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-gold);
  white-space: nowrap;
  margin-left: auto;
  padding-left: 8px;
  flex-shrink: 0;
}

.memory-item {
  background: var(--cp-gold-trace);
  border-left: 2px solid var(--cp-gold-faint);
  border-bottom: 1px dotted var(--cp-gold-faint);
  padding: 10px 14px;
  transition:
    background var(--cp-dur-hover) var(--cp-ease),
    border-left-color var(--cp-dur-hover) var(--cp-ease);
}
.memory-item:hover {
  background: rgba(200, 169, 110, 0.06);
  border-left-color: var(--cp-gold);
}
.memory-text {
  font-family: Georgia, serif;
  font-size: 13px;
  color: rgba(238, 224, 196, 0.88);
  line-height: 1.6;
}
/* Codex inline category — italic small-caps lozenge instead of Vuetify
   chip. Reads as a marginalia tag, not a UI control. */
.memory-cat {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--cp-gold-soft);
  border: 1px solid var(--cp-gold-faint);
  padding: 1px 6px;
  border-radius: 2px;
}
.memory-date {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  color: var(--cp-ink-faint);
  font-variant-numeric: tabular-nums;
}
.memory-overflow {
  margin: 12px 0 0;
  text-align: center;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink-mute);
}

.panel-empty {
  text-align: center;
  margin: 36px auto 0;
  max-width: 36ch;
}
.empty-headline {
  font-family: Georgia, serif;
  font-style: italic;
  color: var(--cp-ink-mute);
  margin: 0 0 6px;
}
.empty-hint {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink-faint);
  line-height: 1.55;
  margin: 0;
}

/* Loading shimmer — three skeleton lines using the global folio-skel
   gradient (Batch A token). Width pattern reads as a paragraph
   silhouette so the panel doesn't reflow when real data arrives. */
.panel-loading {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 4px;
}
.skel-line {
  display: block;
  height: 14px;
  width: 100%;
}
.skel-line.short { width: 64%; }
</style>
