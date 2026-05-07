<template>
  <div class="cp-skel-folio" :aria-busy="true" aria-live="polite">
    <!-- Three ledger ghosts: key + figure pairs that mirror the
         real .ledger-line layout. Heights match the real widget so
         the page doesn't reflow when data arrives. -->
    <div v-for="n in lines" :key="`l-${n}`" class="cp-skel-line">
      <span class="folio-skel cp-skel-key" />
      <span class="folio-skel cp-skel-fig" />
    </div>

    <!-- Two progress tracks. -->
    <template v-if="bars > 0">
      <div v-for="n in bars" :key="`b-${n}`" class="cp-skel-bar-block">
        <div class="cp-skel-bar-head">
          <span class="folio-skel cp-skel-bar-label" />
          <span class="folio-skel cp-skel-bar-fig" />
        </div>
        <span class="folio-skel cp-skel-bar-track" />
      </div>
    </template>

    <p v-if="caption" class="cp-skel-caption">
      <Dotty />
      <span class="cp-skel-caption-text">{{ caption }}</span>
    </p>
  </div>
</template>

<script setup lang="ts">
import Dotty from './Dotty.vue'

withDefaults(defineProps<{
  lines?: number
  bars?: number
  caption?: string
}>(), {
  lines: 3,
  bars: 2,
  caption: 'consulting the rule',
})
</script>

<style scoped>
.cp-skel-folio {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 8px 0;
}

.cp-skel-line {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  border-bottom: 1px dotted var(--cp-rule);
  padding-bottom: 10px;
}

/* Random-feel widths give the skeleton a ledger-line cadence
   instead of the dreaded uniform grey-bar wall. */
.cp-skel-key { height: 13px; width: 38%; }
.cp-skel-line:nth-child(1) .cp-skel-key { width: 42%; }
.cp-skel-line:nth-child(2) .cp-skel-key { width: 36%; }
.cp-skel-line:nth-child(3) .cp-skel-key { width: 44%; }

.cp-skel-fig { height: 14px; width: 22%; }
.cp-skel-line:nth-child(1) .cp-skel-fig { width: 26%; }
.cp-skel-line:nth-child(2) .cp-skel-fig { width: 18%; }
.cp-skel-line:nth-child(3) .cp-skel-fig { width: 24%; }

.cp-skel-bar-block { display: flex; flex-direction: column; gap: 8px; }
.cp-skel-bar-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.cp-skel-bar-label { height: 12px; width: 30%; }
.cp-skel-bar-fig   { height: 12px; width: 18%; }
.cp-skel-bar-track { height: 8px;  width: 100%; }

.cp-skel-caption {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 4px 0 0;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  font-style: italic;
  font-size: 13px;
  letter-spacing: 0.05em;
  color: var(--cp-ink-mute);
}
.cp-skel-caption-text { opacity: 0.85; }
</style>
