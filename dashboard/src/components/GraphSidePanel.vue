<template>
  <v-navigation-drawer
    :model-value="!!node"
    location="right"
    width="420"
    temporary
    scrim="rgba(0,0,0,0.5)"
    class="side-panel"
    @update:model-value="!$event && $emit('close')"
  >
    <template v-if="node">
      <div class="d-flex flex-column fill-height">
        <!-- Header -->
        <div class="pa-4" style="border-bottom: 1px solid rgba(255,255,255,0.06);">
          <div class="d-flex align-center mb-2">
            <EntityBadge :type="node.entity.entity_type" />
            <v-spacer />
            <v-btn icon="mdi-close" variant="text" size="x-small" @click="$emit('close')" />
          </div>
          <div class="text-h6 font-weight-bold" style="line-height: 1.3;">
            {{ node.entity.canonical_name }}
          </div>
          <div v-if="node.entity.description" class="text-body-2 text-medium-emphasis mt-1">
            {{ node.entity.description }}
          </div>
          <div class="d-flex ga-3 mt-3">
            <div class="d-flex align-center ga-1 text-caption text-medium-emphasis">
              <v-icon icon="mdi-eye-outline" size="14" />
              {{ node.entity.mention_count }} mentions
            </div>
            <div class="d-flex align-center ga-1 text-caption text-medium-emphasis">
              <v-icon icon="mdi-clock-outline" size="14" />
              {{ new Date(node.entity.created_at).toLocaleDateString() }}
            </div>
          </div>
        </div>

        <!-- Content -->
        <div class="flex-grow-1 pa-4" style="overflow-y: auto;">
          <!-- Connections -->
          <div v-if="node.connections.length" class="mb-5">
            <div class="d-flex align-center mb-3">
              <v-icon icon="mdi-link-variant" size="16" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-2 font-weight-medium">Connections</span>
              <v-chip size="x-small" variant="tonal" class="ml-2">{{ node.connections.length }}</v-chip>
            </div>
            <div class="connections-list">
              <div
                v-for="(conn, i) in node.connections"
                :key="i"
                class="connection-item d-flex align-center pa-2 rounded-lg"
              >
                <v-icon
                  :icon="conn.direction === 'outgoing' ? 'mdi-arrow-right' : 'mdi-arrow-left'"
                  size="14"
                  :color="conn.direction === 'outgoing' ? 'primary' : 'secondary'"
                  class="mr-2"
                />
                <div class="flex-grow-1 text-body-2" style="min-width: 0;">
                  <v-chip size="x-small" variant="tonal" color="surface-variant" class="mr-1">
                    {{ conn.relation_type }}
                  </v-chip>
                  <EntityBadge :type="conn.target_type" :label="conn.target_name" />
                </div>
                <span class="text-caption text-medium-emphasis ml-2" style="white-space: nowrap;">
                  {{ (conn.confidence * 100).toFixed(0) }}%
                </span>
              </div>
            </div>
          </div>

          <!-- Related Memories -->
          <div v-if="node.memories.length">
            <div class="d-flex align-center mb-3">
              <v-icon icon="mdi-brain" size="16" class="mr-2 text-medium-emphasis" />
              <span class="text-subtitle-2 font-weight-medium">Memories</span>
              <v-chip size="x-small" variant="tonal" class="ml-2">{{ node.memories.length }}</v-chip>
            </div>
            <div class="d-flex flex-column ga-2">
              <v-card
                v-for="mem in node.memories.slice(0, 15)"
                :key="mem.id"
                variant="outlined"
                rounded="lg"
                class="memory-item"
              >
                <v-card-text class="pa-3">
                  <div class="text-body-2 mb-1">
                    {{ mem.summary || mem.content.slice(0, 100) + '...' }}
                  </div>
                  <div class="d-flex align-center ga-2">
                    <v-chip v-if="mem.category" size="x-small" variant="tonal" color="primary">
                      {{ mem.category }}
                    </v-chip>
                    <v-spacer />
                    <span class="text-caption text-medium-emphasis">
                      {{ new Date(mem.created_at).toLocaleDateString() }}
                    </span>
                  </div>
                </v-card-text>
              </v-card>
            </div>
          </div>
        </div>
      </div>
    </template>
  </v-navigation-drawer>
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
.side-panel {
  background: rgb(var(--v-theme-surface)) !important;
}
.connection-item {
  transition: background 100ms ease;
}
.connection-item:hover {
  background: rgba(255, 255, 255, 0.03);
}
.memory-item {
  border-color: rgba(255, 255, 255, 0.04) !important;
  transition: border-color 100ms ease;
}
.memory-item:hover {
  border-color: rgba(255, 255, 255, 0.1) !important;
}
</style>
