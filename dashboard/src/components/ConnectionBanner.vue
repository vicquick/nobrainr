<template>
  <transition name="conn">
    <div v-if="connection.down || connection.hiccup" class="conn-banner" :class="{ down: connection.down }" role="status" aria-live="polite">
      <span class="conn-mark" aria-hidden="true">⸸</span>
      <span v-if="connection.down" class="conn-text">
        The archive does not answer.
        <span class="conn-sub">
          <template v-if="connection.checking">Asking again<Dotty /></template>
          <template v-else>It will be asked again shortly; this page reloads itself when it returns.</template>
        </span>
      </span>
      <span v-else class="conn-text">
        A request went unanswered.
        <span class="conn-sub">What you see may be incomplete.</span>
      </span>
      <button class="conn-act" type="button" :disabled="connection.checking" @click="connection.retryNow()">
        {{ connection.down ? 'ask now' : 'load again' }}
      </button>
    </div>
  </transition>
</template>

<script setup lang="ts">
import Dotty from '@/components/Dotty.vue'
import { useConnectionStore } from '@/stores/connection'

const connection = useConnectionStore()
</script>

<style scoped>
/* Sits under the app bar, above the page, without moving the page: an
   outage notice that shoves the content down reads as a second fault. */
.conn-banner {
  position: fixed; z-index: 1200;
  top: 60px; left: 50%; transform: translateX(-50%);
  width: min(640px, calc(100vw - 24px));
  display: flex; align-items: baseline; gap: 10px;
  padding: 10px 14px;
  background: rgba(20, 17, 10, 0.96);
  border: 1px solid var(--cp-gold-faint);
  border-left: 2px solid var(--cp-gold-soft);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  font-family: Georgia, serif; font-size: 13px; color: var(--cp-ink);
}
.conn-banner.down { border-left-color: #c47a6a; }
.conn-mark { color: var(--cp-gold-soft); }
.conn-banner.down .conn-mark { color: #c47a6a; }
.conn-text { flex: 1; min-width: 0; }
.conn-sub { color: var(--cp-ink-mute); font-style: italic; }
.conn-act {
  flex: none; align-self: center;
  background: none; border: 1px solid var(--cp-gold-faint); color: var(--cp-gold);
  font: inherit; font-style: italic; font-size: 12px; letter-spacing: 0.04em;
  padding: 7px 12px; cursor: pointer;
  transition: border-color var(--cp-dur-hover) var(--cp-ease), background var(--cp-dur-hover) var(--cp-ease);
}
.conn-act:hover:not(:disabled) { border-color: var(--cp-gold); background: var(--cp-gold-trace); }
.conn-act:disabled { opacity: 0.5; cursor: wait; }
.conn-act:focus-visible { outline: 2px solid var(--cp-gold-bright); outline-offset: 2px; }

.conn-enter-active { transition: opacity var(--cp-dur-panel) var(--cp-ease-decel), transform var(--cp-dur-panel) var(--cp-ease-decel); }
.conn-leave-active { transition: opacity var(--cp-dur-out) var(--cp-ease-accel); }
.conn-enter-from { opacity: 0; transform: translate(-50%, -6px); }
.conn-leave-to { opacity: 0; }

@media (max-width: 640px) {
  .conn-banner { flex-wrap: wrap; font-size: 12.5px; }
  .conn-text { flex-basis: calc(100% - 28px); }
  .conn-act { margin-left: auto; padding: 9px 14px; }
}
@media (prefers-reduced-motion: reduce) {
  .conn-enter-active, .conn-leave-active { transition: none; }
}
</style>
