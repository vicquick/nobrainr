<template>
  <v-container fluid class="fill-height pa-0">
    <div class="d-flex fill-height" style="width: 100%;">
      <!-- Left sidebar -->
      <div class="sidebar d-flex flex-column" style="width: 400px; min-width: 400px; border-right: 1px solid rgba(255,255,255,0.1);">
        <div class="pa-3">
          <v-text-field
            v-model="searchQuery"
            prepend-inner-icon="mdi-magnify"
            placeholder="Search memories..."
            variant="outlined"
            density="compact"
            clearable
            hide-details
            class="mb-2"
          />
          <div class="d-flex ga-2">
            <v-select
              v-model="categoryFilter"
              :items="categories"
              label="Category"
              variant="outlined"
              density="compact"
              clearable
              hide-details
              class="flex-grow-1"
            />
            <v-select
              v-model="machineFilter"
              :items="machines"
              label="Machine"
              variant="outlined"
              density="compact"
              clearable
              hide-details
              class="flex-grow-1"
            />
          </div>
        </div>

        <v-divider />

        <div class="flex-grow-1 pa-3" style="overflow-y: auto;">
          <template v-if="loading">
            <v-skeleton-loader v-for="n in 5" :key="n" type="card" class="mb-2" />
          </template>
          <template v-else-if="memories.length">
            <MemoryCard
              v-for="m in memories"
              :key="m.id"
              :memory="m"
              :selected="selectedMemory?.id === m.id"
              @click="selectMemory(m.id)"
            />
          </template>
          <div v-else class="text-center text-medium-emphasis pa-4">
            No memories found
          </div>
        </div>
      </div>

      <!-- Right panel -->
      <div class="flex-grow-1 pa-4" style="overflow-y: auto;">
        <template v-if="detailLoading">
          <v-skeleton-loader type="article" />
        </template>
        <template v-else-if="selectedMemory">
          <MemoryDetail
            :memory="selectedMemory"
            :entities="selectedEntities"
            @update="handleUpdate"
            @delete="handleDelete"
          />
        </template>
        <div v-else class="d-flex align-center justify-center fill-height text-medium-emphasis">
          <div class="text-center">
            <v-icon icon="mdi-brain" size="64" class="mb-2" />
            <div class="text-h6">Select a memory to view details</div>
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

// Debounced search
let searchTimeout: ReturnType<typeof setTimeout>
watch(searchQuery, () => {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => fetchMemories(buildParams()), 300)
})

watch([categoryFilter, machineFilter], () => {
  fetchMemories(buildParams())
})

onMounted(async () => {
  await statsStore.fetchStats()
  fetchCategories()
  fetchMachines()
  fetchMemories()
})
</script>
