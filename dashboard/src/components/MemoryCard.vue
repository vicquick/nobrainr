<template>
  <v-card
    class="memory-card mb-2"
    :class="{ 'memory-card--selected': selected }"
    variant="outlined"
    hover
    @click="$emit('click')"
  >
    <v-card-text class="pa-3">
      <div class="d-flex align-center mb-2">
        <v-chip v-if="memory.category" size="x-small" variant="tonal" color="primary">
          {{ memory.category }}
        </v-chip>
        <v-spacer />
        <span class="text-caption text-medium-emphasis">{{ formattedDate }}</span>
      </div>

      <div class="text-body-2 mb-2">{{ displayText }}</div>

      <div class="d-flex align-center ga-2 flex-wrap">
        <v-chip
          v-if="memory.similarity != null"
          size="x-small"
          variant="tonal"
          color="success"
        >
          {{ (memory.similarity * 100).toFixed(0) }}% match
        </v-chip>
        <v-chip
          v-if="memory.relevance_score != null"
          size="x-small"
          variant="tonal"
          color="secondary"
        >
          rel {{ memory.relevance_score.toFixed(2) }}
        </v-chip>
        <v-chip
          v-for="tag in memory.tags.slice(0, 3)"
          :key="tag"
          size="x-small"
          variant="outlined"
          color="grey"
        >
          {{ tag }}
        </v-chip>
        <span v-if="memory.tags.length > 3" class="text-caption text-medium-emphasis">
          +{{ memory.tags.length - 3 }}
        </span>
      </div>
    </v-card-text>
  </v-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Memory } from '@/types'

const props = defineProps<{
  memory: Memory
  selected?: boolean
}>()

defineEmits<{
  click: []
}>()

const displayText = computed(() => {
  if (props.memory.summary) return props.memory.summary
  return props.memory.content.length > 100
    ? props.memory.content.slice(0, 100) + '...'
    : props.memory.content
})

const formattedDate = computed(() => {
  return new Date(props.memory.created_at).toLocaleDateString()
})
</script>

<style scoped>
.memory-card--selected {
  border-color: rgb(var(--v-theme-primary)) !important;
  border-width: 2px;
}
</style>
