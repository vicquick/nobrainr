<template>
  <v-navigation-drawer
    :model-value="!!node"
    location="right"
    width="400"
    temporary
    @update:model-value="!$event && $emit('close')"
  >
    <template v-if="node">
      <v-card flat class="fill-height d-flex flex-column">
        <v-card-title class="d-flex align-center pa-4">
          <EntityBadge :type="node.entity.entity_type" />
          <span class="ml-2 text-h6">{{ node.entity.canonical_name }}</span>
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" size="small" @click="$emit('close')" />
        </v-card-title>

        <v-divider />

        <v-card-text class="flex-grow-1" style="overflow-y: auto;">
          <!-- Entity Info -->
          <div v-if="node.entity.description" class="text-body-2 mb-3">
            {{ node.entity.description }}
          </div>
          <div class="d-flex ga-4 text-caption text-medium-emphasis mb-4">
            <span>Mentions: {{ node.entity.mention_count }}</span>
            <span>Created: {{ new Date(node.entity.created_at).toLocaleDateString() }}</span>
          </div>

          <!-- Connections -->
          <div v-if="node.connections.length" class="mb-4">
            <div class="text-subtitle-2 mb-2">Connections ({{ node.connections.length }})</div>
            <v-list density="compact" class="bg-transparent">
              <v-list-item v-for="(conn, i) in node.connections" :key="i" class="px-0">
                <template #prepend>
                  <span class="text-body-2 mr-2">{{ conn.direction === 'outgoing' ? '&rarr;' : '&larr;' }}</span>
                </template>
                <v-list-item-title class="text-body-2">
                  <v-chip size="x-small" variant="tonal" class="mr-1">{{ conn.relation_type }}</v-chip>
                  <EntityBadge :type="conn.target_type" :label="conn.target_name" />
                </v-list-item-title>
                <template #append>
                  <span class="text-caption text-medium-emphasis">{{ (conn.confidence * 100).toFixed(0) }}%</span>
                </template>
              </v-list-item>
            </v-list>
          </div>

          <!-- Related Memories -->
          <div v-if="node.memories.length">
            <div class="text-subtitle-2 mb-2">Related Memories ({{ node.memories.length }})</div>
            <v-list density="compact" class="bg-transparent">
              <v-list-item
                v-for="mem in node.memories.slice(0, 10)"
                :key="mem.id"
                class="px-0"
              >
                <v-list-item-title class="text-body-2">
                  {{ mem.summary || mem.content.slice(0, 80) + '...' }}
                </v-list-item-title>
                <v-list-item-subtitle class="text-caption">
                  {{ new Date(mem.created_at).toLocaleDateString() }}
                </v-list-item-subtitle>
              </v-list-item>
            </v-list>
          </div>
        </v-card-text>
      </v-card>
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
