<template>
  <v-chip :color="color" size="small" variant="tonal" density="compact" class="entity-badge">
    <v-icon v-if="icon" :icon="icon" size="10" class="mr-1" />
    {{ label || type }}
  </v-chip>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const TYPE_META: Record<string, { color: string; icon: string }> = {
  person: { color: '#58a6ff', icon: 'mdi-account' },
  project: { color: '#3fb950', icon: 'mdi-folder-outline' },
  technology: { color: '#bc8cff', icon: 'mdi-code-braces' },
  concept: { color: '#f0883e', icon: 'mdi-lightbulb-outline' },
  file: { color: '#8b949e', icon: 'mdi-file-outline' },
  config: { color: '#d29922', icon: 'mdi-cog-outline' },
  error: { color: '#f85149', icon: 'mdi-alert-circle-outline' },
  location: { color: '#3fb950', icon: 'mdi-map-marker-outline' },
  organization: { color: '#58a6ff', icon: 'mdi-domain' },
}

const props = defineProps<{
  type: string
  label?: string
}>()

const color = computed(() => TYPE_META[props.type]?.color || '#8b949e')
const icon = computed(() => TYPE_META[props.type]?.icon || '')
</script>

<style scoped>
.entity-badge {
  font-weight: 500;
  letter-spacing: 0;
}
</style>
