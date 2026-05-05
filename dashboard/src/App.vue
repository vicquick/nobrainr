<template>
  <v-app class="folio-app">
    <AppBar />
    <v-main>
      <router-view v-slot="{ Component }">
        <transition name="folio-fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </v-main>
    <ChatPanel />
  </v-app>
</template>

<script setup lang="ts">
import AppBar from '@/components/AppBar.vue'
import ChatPanel from '@/components/ChatPanel.vue'
</script>

<style>
/* GLOBAL: parchment-dark page with subtle vellum grain. Applied at the
   v-app level so every view inherits the texture without each view
   having to reproduce it. */
/* Page-wide safety: nothing should ever push the page wider than the
   viewport. box-sizing keeps padding inside the declared widths. */
html, body, #app {
  max-width: 100vw;
  overflow-x: hidden;
}
*, *::before, *::after { box-sizing: border-box; }

.folio-app {
  background:
    radial-gradient(1200px 800px at 20% 10%, rgba(200, 169, 110, 0.04), transparent 60%),
    radial-gradient(900px 700px at 90% 90%, rgba(200, 169, 110, 0.03), transparent 60%),
    linear-gradient(180deg, #0e0b06 0%, #14110a 100%) !important;
  color: rgba(238, 224, 196, 0.94);
  font-family: Georgia, 'Palatino Linotype', Palatino, 'Hoefler Text', serif;
  max-width: 100vw;
  overflow-x: hidden;
}

/* Vuetify v-main background override */
.v-application {
  background: transparent !important;
}
.v-main { background: transparent !important; }

/* Page transition — slow folio fade */
.folio-fade-enter-active, .folio-fade-leave-active {
  transition: opacity 200ms cubic-bezier(0.22, 1, 0.36, 1);
}
.folio-fade-enter-from, .folio-fade-leave-to {
  opacity: 0;
}

/* Global scrollbar — gold thread on parchment */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: rgba(14, 11, 6, 0.3); }
::-webkit-scrollbar-thumb {
  background: rgba(200, 169, 110, 0.2);
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover {
  background: rgba(200, 169, 110, 0.4);
  background-clip: padding-box;
  border: 2px solid transparent;
}

/* Selection — gold ink */
::selection {
  background: rgba(200, 169, 110, 0.3);
  color: rgba(238, 224, 196, 0.98);
}

/* Form controls — global tints */
input, textarea, select {
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
}
</style>
