<template>
  <v-container fluid class="fill-height pa-0">
    <div class="d-flex fill-height" style="width: 100%;">
      <!-- Left sidebar -->
      <div class="sidebar d-flex flex-column" style="width: 400px; min-width: 400px; border-right: 1px solid rgba(255,255,255,0.04);">
        <div class="pa-3">
          <v-text-field
            v-model="searchQuery"
            prepend-inner-icon="mdi-magnify"
            placeholder="Search memories..."
            clearable
            class="mb-2"
          />
          <div class="d-flex ga-2">
            <v-select
              v-model="categoryFilter"
              :items="categories"
              label="Category"
              clearable
              class="flex-grow-1"
            />
            <v-select
              v-model="machineFilter"
              :items="machines"
              label="Machine"
              clearable
              class="flex-grow-1"
            />
          </div>
        </div>

        <v-divider style="opacity: 0.3;" />

        <div class="flex-grow-1 pa-3" style="overflow-y: auto;">
          <template v-if="loading">
            <div v-for="n in 6" :key="n" class="skeleton-card mb-2" />
          </template>
          <template v-else-if="memories.length">
            <div class="d-flex flex-column ga-2">
              <MemoryCard
                v-for="m in memories"
                :key="m.id"
                :memory="m"
                :selected="selectedMemory?.id === m.id"
                @click="selectMemory(m.id)"
              />
            </div>
          </template>
          <div v-else class="text-center text-medium-emphasis pa-8">
            <v-icon icon="mdi-magnify-close" size="32" class="mb-2 d-block mx-auto" style="opacity: 0.3;" />
            No memories found
          </div>
        </div>
      </div>

      <!-- Right panel -->
      <div class="flex-grow-1 pa-5" style="overflow-y: auto;">
        <template v-if="detailLoading">
          <div class="skeleton-card" style="height: 200px;" />
        </template>
        <template v-else-if="selectedMemory">
          <MemoryDetail
            :memory="selectedMemory"
            :entities="selectedEntities"
            @update="handleUpdate"
            @delete="handleDelete"
          />
        </template>
        <div v-else class="d-flex align-center justify-center fill-height">
          <div class="text-center">
            <v-icon icon="mdi-brain" size="56" class="mb-3 d-block mx-auto" style="opacity: 0.12;" />
            <div class="text-body-1 text-medium-emphasis">Select a memory to view details</div>
          </div>
        </div>
      </div>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { watch, onMounted } from 'vue'
import { useMemories } from '@/composables/useMemories'
import { useStatsStore } from '@/stores/stats'
import { useSSE } from '@/composables/useSSE'
import MemoryCard from '@/components/MemoryCard.vue'
import MemoryDetail from '@/components/MemoryDetail.vue'

const statsStore = useStatsStore()
const {
  memories,
  selectedMemory,
  selectedEntities,
  loading,
  detailLoading,
  searchQuery,
  categoryFilter,
  machineFilter,
  categories,
  machines,
  fetchMemories,
  fetchMemoryDetail,
  updateMemory,
  deleteMemory,
  fetchCategories,
  fetchMachines,
} = useMemories()

function buildParams() {
  const params: Record<string, string | number> = {}
  if (searchQuery.value) params.q = searchQuery.value
  if (categoryFilter.value) params.category = categoryFilter.value
  if (machineFilter.value) params.source_machine = machineFilter.value
  return params
}

async function selectMemory(id: string) {
  await fetchMemoryDetail(id)
}

async function handleUpdate(body: Record<string, unknown>) {
  if (!selectedMemory.value) return
  await updateMemory(selectedMemory.value.id, body)
  await fetchMemories(buildParams())
}

async function handleDelete() {
  if (!selectedMemory.value) return
  await deleteMemory(selectedMemory.value.id)
  await fetchMemories(buildParams())
}

let searchTimeout: ReturnType<typeof setTimeout>
watch(searchQuery, () => {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => fetchMemories(buildParams()), 300)
})

watch([categoryFilter, machineFilter], () => {
  fetchMemories(buildParams())
})

useSSE((evt) => {
  if (['memory_created', 'memory_updated', 'memory_deleted'].includes(evt.type)) {
    fetchMemories(buildParams())
    fetchCategories()
    fetchMachines()
  }
})

onMounted(async () => {
  await statsStore.fetchStats()
  fetchCategories()
  fetchMachines()
  fetchMemories()
})
</script>

<style scoped>
.skeleton-card {
  background: linear-gradient(90deg, rgb(var(--v-theme-surface)) 25%, rgba(255,255,255,0.03) 50%, rgb(var(--v-theme-surface)) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 12px;
  height: 96px;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
</style>
