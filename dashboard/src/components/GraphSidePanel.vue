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
            class="connection-item d-flex align-center pa-2 rounded-lg"
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
}>()
</script>

<style scoped>
.panel-header {
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
}
.entity-name {
  font-size: 1.2rem;
  font-weight: 700;
  color: rgba(255, 255, 255, 0.95);
  line-height: 1.3;
}
.entity-description {
  font-size: 0.875rem;
  color: rgba(255, 255, 255, 0.55);
  line-height: 1.5;
}
.stat-item {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 0.75rem;
  color: rgba(255, 255, 255, 0.45);
}
.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.8rem;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.7);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.connections-list {
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.connection-item {
  border: 1px solid transparent;
  transition: background 100ms ease, border-color 100ms ease;
}
.connection-item:hover {
  background: rgba(255, 255, 255, 0.04);
  border-color: rgba(255, 255, 255, 0.06);
}
.conn-relation {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.4);
  background: rgba(255, 255, 255, 0.05);
  padding: 1px 6px;
  border-radius: 4px;
  margin-right: 6px;
}
.conn-target {
  font-size: 0.82rem;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.9);
}
.conn-confidence {
  font-size: 0.75rem;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.7);
  background: rgba(255, 255, 255, 0.06);
  padding: 2px 8px;
  border-radius: 6px;
  white-space: nowrap;
  margin-left: 8px;
  flex-shrink: 0;
}
.memory-item {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  transition: background 100ms ease, border-color 100ms ease;
}
.memory-item:hover {
  background: rgba(255, 255, 255, 0.05);
  border-color: rgba(255, 255, 255, 0.1);
}
.memory-text {
  font-size: 0.82rem;
  color: rgba(255, 255, 255, 0.85);
  line-height: 1.5;
}
.memory-date {
  font-size: 0.7rem;
  color: rgba(255, 255, 255, 0.4);
  font-variant-numeric: tabular-nums;
}
</style>
