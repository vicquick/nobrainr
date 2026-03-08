<template>
  <v-chip :color="color" size="small" variant="tonal" density="compact" class="entity-badge">
    <v-icon v-if="icon" :icon="icon" size="10" class="mr-1" />
    {{ label || type }}
  </v-chip>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const TYPE_META: Record<string, { color: string; icon: string }> = {
  person: { color: '#7b8ec8', icon: 'mdi-account' },
  project: { color: '#6ba87a', icon: 'mdi-folder-outline' },
  technology: { color: '#9585c4', icon: 'mdi-code-braces' },
  concept: { color: '#c4a46a', icon: 'mdi-lightbulb-outline' },
  file: { color: '#7a8290', icon: 'mdi-file-outline' },
  config: { color: '#b09060', icon: 'mdi-cog-outline' },
  error: { color: '#c46b6b', icon: 'mdi-alert-circle-outline' },
  location: { color: '#6b9e8f', icon: 'mdi-map-marker-outline' },
  organization: { color: '#7d92b0', icon: 'mdi-domain' },
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
