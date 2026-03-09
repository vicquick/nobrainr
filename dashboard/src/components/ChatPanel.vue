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
          <!-- Inline image preview for user messages -->
          <div v-if="msg.images?.length" class="message-images mb-1">
            <img
              v-for="(img, idx) in msg.images"
              :key="idx"
              :src="'data:image/png;base64,' + img"
              class="message-image-thumb"
              @click="openImagePreview('data:image/png;base64,' + img)"
            />
          </div>

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
        <!-- Image preview -->
        <div v-if="pendingImages.length" class="image-preview-row d-flex ga-2 mb-2">
          <div v-for="(img, idx) in pendingImages" :key="idx" class="image-preview-item">
            <img :src="img.dataUrl" class="preview-thumb" />
            <v-btn
              icon="mdi-close-circle"
              size="x-small"
              variant="text"
              class="preview-remove"
              @click="removeImage(idx)"
            />
          </div>
        </div>
        <!-- Image size error -->
        <div v-if="imageError" class="text-caption text-error mb-1">{{ imageError }}</div>
        <div class="d-flex align-center ga-2">
          <v-btn
            icon="mdi-image-plus"
            size="small"
            variant="text"
            color="grey"
            :disabled="chatStore.isStreaming"
            @click="triggerFileInput"
          />
          <input
            ref="fileInput"
            type="file"
            accept="image/jpeg,image/png,image/gif,image/webp"
            multiple
            class="d-none"
            @change="onFileSelected"
          />
          <v-textarea
            ref="textareaRef"
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
            @paste="onPaste"
          />
          <v-btn
            :icon="isRecording ? 'mdi-stop' : 'mdi-microphone'"
            :color="isRecording ? 'error' : 'default'"
            size="small"
            variant="tonal"
            :disabled="chatStore.isStreaming || isTranscribing"
            :loading="isTranscribing"
            :class="{ 'mic-recording': isRecording }"
            @click="toggleRecording"
          />
          <v-btn
            icon="mdi-send"
            color="primary"
            size="small"
            variant="tonal"
            :disabled="(!input.trim() && !pendingImages.length) || chatStore.isStreaming"
            :loading="chatStore.isStreaming"
            @click="send"
          />
        </div>
        <div v-if="micError" class="text-caption text-error mt-1">{{ micError }}</div>
      </div>
    </div>
    <!-- Full-size image preview dialog -->
    <v-dialog v-model="showImagePreview" max-width="90vw">
      <v-card color="#1a1a2e" class="pa-2">
        <v-btn icon="mdi-close" variant="text" size="small" class="float-right" @click="showImagePreview = false" />
        <v-img :src="previewImageSrc" max-height="80vh" contain />
      </v-card>
    </v-dialog>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { useDisplay } from 'vuetify'
import { useChatStore } from '@/stores/chat'
import type { ChatSources } from '@/types'

const TYPE_COLORS: Record<string, string> = {
  person: '#7b8ec8', project: '#6ba87a', technology: '#9585c4',
  concept: '#c4a46a', file: '#7a8290', config: '#b09060',
  error: '#c46b6b', location: '#6b9e8f', organization: '#7d92b0',
}

const MAX_IMAGE_SIZE = 10 * 1024 * 1024 // 10 MB
const ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']

interface PendingImage {
  dataUrl: string   // for preview display
  base64: string    // raw base64 without data: prefix (for API)
}

const chatStore = useChatStore()
const { mobile } = useDisplay()

const input = ref('')
const messagesContainer = ref<HTMLElement | null>(null)
const expandedSources = ref(new Set<string>())
const fileInput = ref<HTMLInputElement | null>(null)
const textareaRef = ref<InstanceType<any> | null>(null)
const pendingImages = ref<PendingImage[]>([])
const imageError = ref('')
const showImagePreview = ref(false)
const previewImageSrc = ref('')

// Voice recording state
const isRecording = ref(false)
const isTranscribing = ref(false)
const micError = ref('')
let mediaRecorder: MediaRecorder | null = null
let micStream: MediaStream | null = null
let audioChunks: Blob[] = []

// Cleanup mic resources on unmount
onBeforeUnmount(() => {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  if (micStream) {
    micStream.getTracks().forEach(t => t.stop())
    micStream = null
  }
  mediaRecorder = null
})

async function toggleRecording() {
  if (isRecording.value) {
    stopRecording()
  } else {
    await startRecording()
  }
}

async function startRecording() {
  micError.value = ''
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    micStream = stream

    // Pick a supported mimeType (Safari doesn't support audio/webm)
    let mimeType = 'audio/webm'
    if (!MediaRecorder.isTypeSupported('audio/webm')) {
      if (MediaRecorder.isTypeSupported('audio/mp4')) {
        mimeType = 'audio/mp4'
      } else {
        stream.getTracks().forEach(t => t.stop())
        micStream = null
        micError.value = 'Audio recording not supported in this browser'
        return
      }
    }

    mediaRecorder = new MediaRecorder(stream, { mimeType })
    audioChunks = []

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) audioChunks.push(event.data)
    }

    mediaRecorder.onstop = async () => {
      // Stop all tracks to release the microphone
      stream.getTracks().forEach(t => t.stop())
      micStream = null

      if (audioChunks.length === 0) return
      const ext = mimeType === 'audio/mp4' ? 'mp4' : 'webm'
      const blob = new Blob(audioChunks, { type: mimeType })
      await transcribe(blob, `recording.${ext}`)
    }

    mediaRecorder.start()
    isRecording.value = true
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'NotAllowedError') {
      micError.value = 'Microphone permission denied'
    } else if (err instanceof DOMException && err.name === 'NotFoundError') {
      micError.value = 'No microphone found'
    } else {
      micError.value = 'Could not access microphone'
    }
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  isRecording.value = false
}

async function transcribe(blob: Blob, filename = 'recording.webm') {
  isTranscribing.value = true
  micError.value = ''
  try {
    const formData = new FormData()
    formData.append('file', blob, filename)

    const baseUrl = import.meta.env.VITE_API_BASE || ''
    const res = await fetch(`${baseUrl}/api/transcribe`, {
      method: 'POST',
      body: formData,
    })

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Transcription failed' }))
      micError.value = err.error || 'Transcription failed'
      return
    }

    const data = await res.json()
    if (data.text) {
      input.value = input.value ? `${input.value} ${data.text}` : data.text
    }
  } catch {
    micError.value = 'Could not reach transcription service'
  } finally {
    isTranscribing.value = false
  }
}

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

function openImagePreview(src: string) {
  previewImageSrc.value = src
  showImagePreview.value = true
}

function triggerFileInput() {
  fileInput.value?.click()
}

function processFile(file: File): Promise<PendingImage | null> {
  return new Promise((resolve) => {
    if (!ACCEPTED_TYPES.includes(file.type)) {
      imageError.value = `Unsupported format: ${file.type}. Use JPEG, PNG, GIF, or WebP.`
      resolve(null)
      return
    }
    if (file.size > MAX_IMAGE_SIZE) {
      imageError.value = `Image too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max 10 MB.`
      resolve(null)
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = reader.result as string
      // Strip data:image/...;base64, prefix for API
      const base64 = dataUrl.replace(/^data:image\/[^;]+;base64,/, '')
      resolve({ dataUrl, base64 })
    }
    reader.onerror = () => resolve(null)
    reader.readAsDataURL(file)
  })
}

async function onFileSelected(event: Event) {
  imageError.value = ''
  const target = event.target as HTMLInputElement
  if (!target.files?.length) return
  for (const file of Array.from(target.files)) {
    const img = await processFile(file)
    if (img) pendingImages.value.push(img)
  }
  // Reset input so same file can be re-selected
  target.value = ''
}

async function onPaste(event: ClipboardEvent) {
  const items = event.clipboardData?.items
  if (!items) return
  for (const item of Array.from(items)) {
    if (item.type.startsWith('image/')) {
      event.preventDefault()
      imageError.value = ''
      const file = item.getAsFile()
      if (!file) continue
      const img = await processFile(file)
      if (img) pendingImages.value.push(img)
    }
  }
}

function removeImage(idx: number) {
  pendingImages.value.splice(idx, 1)
  imageError.value = ''
}

function send() {
  if ((!input.value.trim() && !pendingImages.value.length) || chatStore.isStreaming) return
  const images = pendingImages.value.length
    ? pendingImages.value.map(img => img.base64)
    : undefined
  const text = input.value.trim() || (images ? 'What is in this image?' : '')
  chatStore.sendMessage(text, images)
  input.value = ''
  pendingImages.value = []
  imageError.value = ''
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
.mic-recording {
  animation: pulse-recording 1.2s ease-in-out infinite;
}
@keyframes pulse-recording {
  0%, 100% { box-shadow: 0 0 0 0 rgba(244, 67, 54, 0.4); }
  50% { box-shadow: 0 0 0 8px rgba(244, 67, 54, 0); }
}
/* Image preview in input area */
.image-preview-row {
  flex-wrap: wrap;
}
.image-preview-item {
  position: relative;
  display: inline-block;
}
.preview-thumb {
  width: 56px;
  height: 56px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.12);
}
.preview-remove {
  position: absolute;
  top: -6px;
  right: -6px;
  background: rgba(18, 18, 26, 0.85) !important;
}
/* Image thumbnails inside message bubbles */
.message-images {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.message-image-thumb {
  width: 120px;
  max-height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  cursor: pointer;
  transition: opacity 0.15s;
}
.message-image-thumb:hover {
  opacity: 0.8;
}
</style>
