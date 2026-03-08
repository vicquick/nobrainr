<template>
  <v-card
    class="memory-card"
    :class="{ 'memory-card--selected': selected }"
    variant="flat"
    hover
    @click="$emit('click')"
  >
    <v-card-text class="pa-3">
      <div class="d-flex align-center mb-2">
        <v-chip v-if="memory.category" size="x-small" variant="tonal" color="primary" class="font-weight-medium">
          {{ memory.category }}
        </v-chip>
        <v-spacer />
        <span class="text-caption text-medium-emphasis" style="font-variant-numeric: tabular-nums;">
          {{ formattedDate }}
        </span>
      </div>

      <div class="text-body-2 mb-2" style="line-height: 1.5;">{{ displayText }}</div>

      <div class="d-flex align-center ga-1 flex-wrap">
        <v-chip
          v-if="memory.quality_score != null"
          size="x-small"
          variant="tonal"
          :color="qualityColor"
          class="font-weight-medium"
        >
          {{ qualityLabel }}
        </v-chip>
        <v-chip
          v-if="memory.similarity != null"
          size="x-small"
          variant="tonal"
          color="success"
          class="font-weight-medium"
        >
          {{ (memory.similarity * 100).toFixed(0) }}% match
        </v-chip>
        <v-chip
          v-if="memory.relevance_score != null"
          size="x-small"
          variant="tonal"
          color="secondary"
          class="font-weight-medium"
        >
          {{ memory.relevance_score.toFixed(2) }}
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
  return props.memory.content.length > 120
    ? props.memory.content.slice(0, 120) + '...'
    : props.memory.content
})

const formattedDate = computed(() => {
  return new Date(props.memory.created_at).toLocaleDateString()
})

const qualityColor = computed(() => {
  const q = props.memory.quality_score ?? 0
  if (q >= 0.8) return 'amber-darken-1'
  if (q >= 0.6) return 'light-green'
  if (q >= 0.4) return 'grey'
  return 'grey-darken-1'
})

const qualityLabel = computed(() => {
  const q = props.memory.quality_score ?? 0
  return `Q ${(q * 100).toFixed(0)}%`
})
</script>

<style scoped>
.memory-card {
  border: 1px solid rgba(255, 255, 255, 0.04);
  transition: all 150ms ease;
}
.memory-card:hover {
  border-color: rgba(255, 255, 255, 0.1);
}
.memory-card--selected {
  border-color: rgb(var(--v-theme-primary)) !important;
  background: rgba(var(--v-theme-primary), 0.04) !important;
}
</style>
