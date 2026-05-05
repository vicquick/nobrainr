<template>
  <span class="entity-badge" :class="`type-${type}`" :title="label || type">
    <span v-if="glyph" class="entity-glyph">{{ glyph }}</span>
    <span class="entity-label">{{ label || type }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

// Folio glyph per entity type — small typographic marks rather than
// MDI icons. Avoids the "rounded icon above every label" AI tell.
const TYPE_META: Record<string, { glyph: string }> = {
  person: { glyph: '☙' },
  project: { glyph: '◎' },
  technology: { glyph: '⌘' },
  concept: { glyph: '✦' },
  file: { glyph: '¶' },
  config: { glyph: '⚙' },
  error: { glyph: '⚠' },
  location: { glyph: '⌖' },
  organization: { glyph: '⊕' },
  service: { glyph: '◊' },
  command: { glyph: '›' },
  database: { glyph: '◫' },
  date: { glyph: '⌚' },
}

const props = defineProps<{
  type: string
  label?: string
}>()

const glyph = computed(() => TYPE_META[props.type]?.glyph || '·')
</script>

<style scoped>
.entity-badge {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  padding: 2px 8px;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: rgba(238, 224, 196, 0.7);
  background: rgba(200, 169, 110, 0.06);
  border: 1px solid rgba(200, 169, 110, 0.18);
  cursor: default;
  transition: all 150ms;
}
.entity-badge:hover {
  background: rgba(200, 169, 110, 0.12);
  border-color: rgba(200, 169, 110, 0.35);
  color: rgba(238, 224, 196, 0.95);
}
.entity-glyph {
  font-style: normal;
  font-size: 12px;
}
.entity-label { letter-spacing: 0.04em; }

/* Per-type ink colors */
.type-person { color: #b6c2e6; border-color: rgba(123, 142, 200, 0.4); }
.type-person .entity-glyph { color: #b6c2e6; }

.type-project { color: #a8d4ad; border-color: rgba(107, 168, 122, 0.4); }
.type-project .entity-glyph { color: #a8d4ad; }

.type-technology { color: #c5b9e2; border-color: rgba(149, 133, 196, 0.4); }
.type-technology .entity-glyph { color: #c5b9e2; }

.type-concept { color: #e6cc91; border-color: rgba(196, 164, 106, 0.5); }
.type-concept .entity-glyph { color: #e6cc91; }

.type-file { color: #b8c0cc; border-color: rgba(122, 130, 144, 0.4); }
.type-file .entity-glyph { color: #b8c0cc; }

.type-config { color: #d4ba8a; border-color: rgba(176, 144, 96, 0.4); }
.type-config .entity-glyph { color: #d4ba8a; }

.type-error { color: #e8a59f; border-color: rgba(196, 107, 107, 0.4); }
.type-error .entity-glyph { color: #e8a59f; }

.type-location { color: #98c8b8; border-color: rgba(107, 158, 143, 0.4); }
.type-location .entity-glyph { color: #98c8b8; }

.type-organization { color: #b0c0d6; border-color: rgba(125, 146, 176, 0.4); }
.type-organization .entity-glyph { color: #b0c0d6; }
</style>
