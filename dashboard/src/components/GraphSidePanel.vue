<template>
  <div v-if="node" class="d-flex flex-column fill-height">
    <!-- Header -->
    <div class="panel-header pa-4">
      <div class="d-flex align-center mb-2">
        <EntityBadge :type="node.entity.entity_type" />
        <v-spacer />
        <v-btn icon="mdi-close" variant="text" size="x-small" @click="$emit('close')" />
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
      <!-- Connections -->
      <div v-if="node.connections.length" class="mb-5">
        <div class="section-header mb-3">
          <v-icon icon="mdi-link-variant" size="15" />
          <span>Connections</span>
          <v-chip size="x-small" variant="tonal" color="primary" class="ml-2">{{ node.connections.length }}</v-chip>
        </div>
        <div class="connections-list">
          <div
            v-for="(conn, i) in node.connections"
            :key="i"
            class="connection-item d-flex align-center pa-2 rounded-lg clickable"
            @click="$emit('navigate', conn.connected_id)"
            title="Click to explore"
          >
            <v-icon
              :icon="conn.direction === 'outgoing' ? 'mdi-arrow-right' : 'mdi-arrow-left'"
              size="13"
              :color="conn.direction === 'outgoing' ? '#7b8ec8' : '#6ba87a'"
              class="mr-2 flex-shrink-0"
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
          <v-chip size="x-small" variant="tonal" color="primary" class="ml-2">{{ node.memories.length }}</v-chip>
        </div>
        <div class="d-flex flex-column ga-2">
          <div
            v-for="mem in node.memories.slice(0, 15)"
            :key="mem.id"
            class="memory-item pa-3 rounded-lg"
          >
            <div class="memory-text mb-1">
              {{ mem.summary || mem.content.slice(0, 120) + '...' }}
            </div>
            <div class="d-flex align-center ga-2">
              <v-chip v-if="mem.category" size="x-small" variant="tonal" color="primary">
                {{ mem.category }}
              </v-chip>
              <v-spacer />
              <span class="memory-date">
                {{ new Date(mem.created_at).toLocaleDateString() }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { NodeDetail } from '@/types'
import EntityBadge from './EntityBadge.vue'

defineProps<{
  node: NodeDetail | null
}>()

defineEmits<{
  close: []
  navigate: [entityId: string]
}>()
</script>

<style scoped>
/* Graph side-panel rendered as folio marginalia — gold rules, italic
   serif throughout, no card chrome. */
.panel-header {
  border-bottom: 1px solid rgba(200, 169, 110, 0.25);
  background: rgba(200, 169, 110, 0.04);
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
  color: rgba(238, 224, 196, 0.55);
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
  color: #c8a96e;
  font-weight: 400;
  padding-bottom: 6px;
  border-bottom: 1px solid rgba(200, 169, 110, 0.18);
}
.connections-list {
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.connection-item {
  border-bottom: 1px dotted rgba(200, 169, 110, 0.18);
  padding: 8px 6px;
  transition: all 150ms;
  display: flex;
  align-items: baseline;
  font-family: Georgia, serif;
}
.connection-item:hover {
  padding-left: 10px;
  background: rgba(200, 169, 110, 0.04);
}
.clickable { cursor: pointer; }
.conn-relation {
  display: inline-block;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: rgba(238, 224, 196, 0.55);
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
  color: #c8a96e;
  white-space: nowrap;
  margin-left: auto;
  padding-left: 8px;
  flex-shrink: 0;
}
.memory-item {
  background: rgba(200, 169, 110, 0.03);
  border-left: 2px solid rgba(200, 169, 110, 0.25);
  border-bottom: 1px dotted rgba(200, 169, 110, 0.18);
  padding: 10px 14px;
  transition: all 150ms;
}
.memory-item:hover {
  background: rgba(200, 169, 110, 0.06);
  border-left-color: #c8a96e;
}
.memory-text {
  font-family: Georgia, serif;
  font-size: 13px;
  color: rgba(238, 224, 196, 0.88);
  line-height: 1.6;
}
.memory-date {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  color: rgba(238, 224, 196, 0.5);
  font-variant-numeric: tabular-nums;
}
</style>
