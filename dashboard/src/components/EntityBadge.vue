<template>
  <v-chip :color="color" size="small" variant="flat" density="compact">
    {{ label || type }}
  </v-chip>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const TYPE_COLORS: Record<string, string> = {
  person: '#58a6ff',
  project: '#3fb950',
  technology: '#bc8cff',
  concept: '#f0883e',
  file: '#8b949e',
  config: '#d29922',
  error: '#f85149',
  location: '#3fb950',
  organization: '#58a6ff',
}

const props = defineProps<{
  type: string
  label?: string
}>()

const color = computed(() => TYPE_COLORS[props.type] || '#8b949e')
</script>
