<template>
  <div class="galaxy-view">
    <KnowledgeGalaxy />
  </div>
</template>

<script setup lang="ts">
import KnowledgeGalaxy from '@/components/KnowledgeGalaxy.vue'
</script>

<style scoped>
.galaxy-view {
  width: 100%;
  height: calc(100vh - 48px);
  background: #000;
}
</style>
