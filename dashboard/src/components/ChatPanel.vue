<template>
  <v-navigation-drawer
    v-model="chatStore.isOpen"
    location="right"
    :width="mobile ? undefined : 420"
    temporary
    scrim="rgba(0,0,0,0.3)"
    class="chat-panel"
    :class="{ 'chat-panel-mobile': mobile }"
  >
    <div class="d-flex flex-column fill-height">
      <!-- Header -->
      <div class="chat-header d-flex align-center pa-3">
        <v-icon icon="mdi-chat-outline" size="18" class="mr-2" color="primary" />
        <span class="text-subtitle-2 font-weight-bold">Knowledge Chat</span>
        <v-spacer />
        <v-btn
          icon="mdi-delete-outline"
          variant="text"
          size="x-small"
          :disabled="chatStore.messages.length === 0"
          @click="chatStore.clearHistory()"
        />
        <v-btn icon="mdi-close" variant="text" size="x-small" @click="chatStore.close()" />
      </div>

      <!-- Messages -->
      <div ref="messagesContainer" class="flex-grow-1 messages-area pa-3">
        <div v-if="chatStore.messages.length === 0" class="empty-state">
          <v-icon icon="mdi-brain" size="40" color="primary" class="mb-3" style="opacity: 0.3;" />
          <div class="text-caption text-medium-emphasis">
            Ask questions about the knowledge base
          </div>
        </div>

        <div
          v-for="msg in chatStore.messages"
          :key="msg.id"
          class="message-bubble"
          :class="msg.role"
        >
          <div v-if="chatStore.isThinking && msg === lastAssistantMsg && !msg.content" class="thinking-state">
            <v-progress-circular indeterminate size="14" width="1.5" color="primary" class="mr-2" />
            <span class="text-caption text-medium-emphasis">Searching knowledge base...</span>
          </div>
          <div v-else class="message-content">{{ msg.content }}<span v-if="chatStore.isStreaming && msg === lastAssistantMsg && !msg.content" class="typing-dot" /></div>

          <!-- Sources -->
          <div v-if="msg.sources && (msg.sources.entities.length || msg.sources.memories.length)" class="sources-section mt-2">
            <div
              class="sources-toggle d-flex align-center"
              @click="toggleSources(msg.id)"
            >
              <v-icon icon="mdi-link-variant" size="12" class="mr-1" />
              <span>{{ msg.sources.entities.length }} entities · {{ msg.sources.memories.length }} memories</span>
              <v-icon
                :icon="expandedSources.has(msg.id) ? 'mdi-chevron-up' : 'mdi-chevron-down'"
                size="14"
                class="ml-1"
              />
            </div>
            <div v-if="expandedSources.has(msg.id)" class="sources-list mt-1">
              <div
                v-for="entity in msg.sources.entities"
                :key="entity.id"
                class="source-entity"
                @click="focusSingleEntity(entity.id)"
              >
                <span class="entity-type-dot" :style="{ background: typeColor(entity.entity_type) }" />
                {{ entity.name }}
              </div>
              <div
                v-for="mem in msg.sources.memories.slice(0, 5)"
                :key="mem.id"
                class="source-memory"
              >
                {{ mem.summary || mem.content }}
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Input -->
      <div class="chat-input pa-3">
        <div class="d-flex align-center ga-2">
          <v-textarea
            v-model="input"
            placeholder="Ask about the knowledge base..."
            variant="outlined"
            density="compact"
            rows="1"
            max-rows="4"
            auto-grow
            hide-details
            class="flex-grow-1"
            @keydown.enter.exact.prevent="send"
          />
          <v-btn
            icon="mdi-send"
            color="primary"
            size="small"
            variant="tonal"
            :disabled="!input.trim() || chatStore.isStreaming"
            :loading="chatStore.isStreaming"
            @click="send"
          />
        </div>
      </div>
    </div>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useDisplay } from 'vuetify'
import { useChatStore } from '@/stores/chat'
import type { ChatSources } from '@/types'

const TYPE_COLORS: Record<string, string> = {
  person: '#7b8ec8', project: '#6ba87a', technology: '#9585c4',
  concept: '#c4a46a', file: '#7a8290', config: '#b09060',
  error: '#c46b6b', location: '#6b9e8f', organization: '#7d92b0',
}

const chatStore = useChatStore()
const { mobile } = useDisplay()

const input = ref('')
const messagesContainer = ref<HTMLElement | null>(null)
const expandedSources = ref(new Set<string>())

const lastAssistantMsg = computed(() => {
  const msgs = chatStore.messages
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') return msgs[i]
  }
  return null
})

function typeColor(type: string) {
  return TYPE_COLORS[type] || '#7a8290'
}

function toggleSources(msgId: string) {
  const s = new Set(expandedSources.value)
  if (s.has(msgId)) s.delete(msgId)
  else s.add(msgId)
  expandedSources.value = s
}

function highlightEntity(sources: ChatSources) {
  chatStore.currentSources = sources
}

function focusSingleEntity(entityId: string) {
  chatStore.focusEntity(entityId)
}

function send() {
  if (!input.value.trim() || chatStore.isStreaming) return
  chatStore.sendMessage(input.value)
  input.value = ''
}

// Auto-scroll on new messages
watch(
  () => chatStore.messages[chatStore.messages.length - 1]?.content,
  async () => {
    await nextTick()
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  },
)
</script>

<style scoped>
.chat-panel {
  background: #12121a !important;
  border-left: 1px solid rgba(255, 255, 255, 0.08) !important;
}
.chat-panel-mobile {
  width: 100% !important;
}
.chat-header {
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
  min-height: 48px;
}
.messages-area {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  opacity: 0.7;
}
.message-bubble {
  max-width: 92%;
  padding: 8px 12px;
  border-radius: 10px;
  font-size: 0.84rem;
  line-height: 1.55;
  word-break: break-word;
}
.message-bubble.user {
  align-self: flex-end;
  background: rgba(123, 142, 200, 0.15);
  border: 1px solid rgba(123, 142, 200, 0.2);
  color: rgba(255, 255, 255, 0.9);
}
.message-bubble.assistant {
  align-self: flex-start;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.85);
}
.message-content {
  white-space: pre-wrap;
}
.thinking-state {
  display: flex;
  align-items: center;
  padding: 2px 0;
}
.typing-dot::after {
  content: '...';
  animation: blink 1s steps(3) infinite;
}
@keyframes blink {
  0%, 100% { opacity: 0.2; }
  50% { opacity: 1; }
}
.sources-section {
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  padding-top: 6px;
}
.sources-toggle {
  font-size: 0.7rem;
  color: rgba(255, 255, 255, 0.45);
  cursor: pointer;
  user-select: none;
}
.sources-toggle:hover {
  color: rgba(255, 255, 255, 0.65);
}
.sources-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.source-entity {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.72rem;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
}
.source-entity:hover {
  background: rgba(255, 255, 255, 0.06);
}
.entity-type-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  flex-shrink: 0;
}
.source-memory {
  font-size: 0.7rem;
  color: rgba(255, 255, 255, 0.55);
  padding: 2px 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-input {
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
}
</style>
