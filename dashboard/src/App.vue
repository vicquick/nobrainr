<template>
  <v-app>
    <AppBar />
    <v-main>
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
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
.fade-enter-active,
.fade-leave-active {
  transition: opacity 150ms ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
