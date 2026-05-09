<template>
  <article
    class="folio-card"
    :class="{ selected }"
    @click="$emit('click')"
  >
    <div class="card-margin">
      <span class="card-glyph">·</span>
      <span class="card-date">{{ formattedDate }}</span>
    </div>

    <div class="card-body">
      <!-- v-html is safe here: highlightMatches HTML-escapes both the
           source text and the query before constructing the <mark>
           wrappers. See composables/useHighlight.ts. -->
      <p class="card-text" v-html="displayHtml" />

      <div class="card-meta">
        <span v-if="memory.category" class="meta-tag">{{ memory.category }}</span>

        <span v-if="memory.quality_score != null" class="meta-fig" :class="qualityClass">
          q · {{ Math.round((memory.quality_score ?? 0) * 100) }}<span class="pct">%</span>
        </span>
        <span v-if="memory.similarity != null" class="meta-fig accent-good">
          sim · {{ Math.round(memory.similarity * 100) }}<span class="pct">%</span>
        </span>
        <span v-if="memory.relevance_score != null" class="meta-fig">
          rel · {{ memory.relevance_score.toFixed(2) }}
        </span>

        <span v-if="memory.tags?.length" class="meta-tags">
          <span v-for="(tag, i) in memory.tags.slice(0, 3)" :key="tag">
            <span v-if="i > 0" class="tag-sep">·</span>
            <em>{{ tag }}</em>
          </span>
          <span v-if="memory.tags.length > 3" class="tag-more">+{{ memory.tags.length - 3 }}</span>
        </span>
      </div>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Memory } from '@/types'
import { highlightMatches } from '@/composables/useHighlight'

const props = defineProps<{
  memory: Memory
  selected?: boolean
  /**
   * Optional active search query. When present, occurrences of each
   * whitespace-separated token are wrapped in <mark class="cp-mark">.
   * Both source text and query are HTML-escaped before the wrap, so
   * v-html'ing the result is safe even when memory content contains
   * tag-like substrings.
   */
  highlight?: string | null
}>()

defineEmits<{
  click: []
}>()

const displayText = computed(() => {
  if (props.memory.summary) return props.memory.summary
  return props.memory.content.length > 140
    ? props.memory.content.slice(0, 140) + '…'
    : props.memory.content
})

const displayHtml = computed(() => highlightMatches(displayText.value, props.highlight))

const formattedDate = computed(() => {
  return new Date(props.memory.created_at).toLocaleDateString('en-GB', {
    day: 'numeric', month: 'short',
  })
})

const qualityClass = computed(() => {
  const q = props.memory.quality_score ?? 0
  if (q >= 0.7) return 'accent-good'
  if (q >= 0.4) return 'accent-mid'
  return 'accent-low'
})
</script>

<style scoped>
.folio-card {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 12px;
  padding: 12px 8px;
  border-bottom: 1px dotted rgba(200, 169, 110, 0.18);
  cursor: pointer;
  transition: all 180ms cubic-bezier(0.22, 1, 0.36, 1);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
.folio-card:hover {
  padding-left: 12px;
  background: rgba(200, 169, 110, 0.03);
}
.folio-card.selected {
  background: rgba(200, 169, 110, 0.06);
  border-left: 2px solid #c8a96e;
  padding-left: 14px;
}
.folio-card.selected .card-glyph { color: #c8a96e; }

/* Marginalia */
.card-margin {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  padding-right: 12px;
  border-right: 1px solid rgba(200, 169, 110, 0.18);
  text-align: right;
}
.card-glyph {
  font-family: Georgia, serif;
  font-size: 16px;
  color: rgba(200, 169, 110, 0.45);
  line-height: 1;
}
.card-date {
  font-family: Georgia, serif;
  font-size: 10px;
  font-style: italic;
  color: rgba(238, 224, 196, 0.55);
  letter-spacing: 0.05em;
  white-space: nowrap;
}

/* Body */
.card-body { min-width: 0; }
.card-text {
  font-family: Georgia, serif;
  font-size: 13px;
  line-height: 1.55;
  color: rgba(238, 224, 196, 0.94);
  margin: 0 0 6px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.folio-card.selected .card-text { color: rgba(238, 224, 196, 0.98); }

.card-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  font-family: Georgia, serif;
  font-size: 11px;
}
.meta-tag {
  font-style: italic;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #c8a96e;
  font-size: 10px;
}
.meta-fig {
  font-style: italic;
  color: rgba(238, 224, 196, 0.55);
  font-variant-numeric: tabular-nums;
}
.meta-fig em { color: rgba(238, 224, 196, 0.9); font-style: normal; }
.meta-fig.accent-good { color: #8aa96e; }
.meta-fig.accent-mid { color: #c89e6e; }
.meta-fig.accent-low { color: rgba(238, 224, 196, 0.45); }
.pct { font-size: 10px; }

.meta-tags {
  font-style: italic;
  color: rgba(238, 224, 196, 0.55);
  font-size: 11px;
  display: inline-flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px;
}
.meta-tags em { color: rgba(238, 224, 196, 0.7); font-style: italic; }
.tag-sep { color: rgba(200, 169, 110, 0.45); }
.tag-more { color: rgba(200, 169, 110, 0.55); margin-left: 4px; }
</style>
